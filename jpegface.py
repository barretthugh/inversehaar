import collections
import sys
import xml.etree.ElementTree

import cv2
import numpy

from docplex.mp.context import DOcloudContext
from docplex.mp.environment import Environment
from docplex.mp.model import Model

DOCLOUD_URL = 'https://api-oaas.docloud.ibmcloud.com/job_manager/rest/v1/'
docloud_context = DOcloudContext.make_default_context(DOCLOUD_URL)
docloud_context.print_information()
env = Environment()
env.print_information()

BASIC_QUANT_TABLE = numpy.array([
    [16,  11,  10,  16,  24,  40,  51,  61],
    [12,  12,  14,  19,  26,  58,  60,  55],
    [14,  13,  16,  24,  40,  57,  69,  56],
    [14,  17,  22,  29,  51,  87,  80,  62],
    [18,  22,  37,  56,  68, 109, 103,  77],
    [24,  35,  55,  64,  81, 104, 113,  92],
    [49,  64,  78,  87, 103, 121, 120, 101],
    [72,  92,  95,  98, 112, 100, 103,  99]
])


def make_quant_table(quality):
    # Clamp quality to 1 <= quality <= 100
    quality = min(100, max(1, quality))

    # Scale factor is then defined piece-wise, and is inversely related to
    # quality.
    if quality < 50:
        scale_factor = 5000 // quality
    else:
        scale_factor = 200 - quality * 2

    out = numpy.clip((BASIC_QUANT_TABLE * scale_factor + 50) / 100, 0, 255)
                
    assert out.dtype == numpy.int32
    return out


Stage = collections.namedtuple('Stage', ['threshold', 'weak_classifiers'])
WeakClassifier = collections.namedtuple('WeakClassifier',
                          ['feature_idx', 'threshold', 'fail_val', 'pass_val'])
Rect = collections.namedtuple('Rect', ['x', 'y', 'w', 'h', 'weight'])


class Cascade(collections.namedtuple('_CascadeBase',
                                 ['width', 'height', 'stages', 'features'])):

    @staticmethod
    def _split_text_content(n):
        return n.text.strip().split(' ')

    @classmethod
    def load(cls, fname):
        root = xml.etree.ElementTree.parse(fname)

        width = int(root.find('./cascade/width').text.strip())
        height = int(root.find('./cascade/height').text.strip())

        stages = []
        for stage_node in root.findall('./cascade/stages/_'):
            stage_threshold = float(
                              stage_node.find('./stageThreshold').text.strip())
            weak_classifiers = []
            for classifier_node in stage_node.findall('weakClassifiers/_'):
                sp = cls._split_text_content(
                                       classifier_node.find('./internalNodes'))
                assert sp[0] == "0"
                assert sp[1] == "-1"
                feature_idx = int(sp[2])
                threshold = float(sp[3])

                sp = cls._split_text_content(
                                          classifier_node.find('./leafValues'))
                fail_val = float(sp[0])
                pass_val = float(sp[1])
                weak_classifiers.append(
                    WeakClassifier(feature_idx, threshold, fail_val, pass_val))
            stages.append(Stage(stage_threshold, weak_classifiers))

        features = []
        for feature_node in root.findall('./cascade/features/_'):
            feature = []
            for rect_node in feature_node.findall('./rects/_'):
                sp = cls._split_text_content(rect_node)
                x, y, w, h = (int(x) for x in sp[:4])
                weight = float(sp[4])
                feature.append(Rect(x, y, w, h, weight))
            features.append(feature)

        stages = stages[:]

        return cls(width, height, stages, features)

    def feature_to_array(self, feature_idx):
        out = numpy.zeros((self.height, self.width))
        feature = self.features[feature_idx]
        for x, y, w, h, weight in feature:
            out[y:(y + h), x:(x + w)] += weight
        return out

    def detect(self, im):
        if im.shape != (self.height, self.width):
            im = cv2.resize(im, (self.width, self.height),
                           interpolation=cv2.INTER_AREA)

        im = im.astype(numpy.float64)

        #im /= numpy.std(im) * (self.height * self.width)
        im /= 256. * (self.height * self.width)

        for stage_idx, stage in enumerate(self.stages):
            total = 0
            for classifier in stage.weak_classifiers:
                feature_array = self.feature_to_array(classifier.feature_idx)
                if numpy.sum(feature_array * im) >= classifier.threshold:
                    total += classifier.pass_val
                else:
                    total += classifier.fail_val

            if total < stage.threshold:
                print "Bailing out at stage {}".format(stage_idx)
                return -stage_idx
        return 1


class CascadeModel(object):
    def __init__(self, cascade, epsilon=0.00000):
        model = Model("Inverse haar cascade", docloud_context=docloud_context)

        pixel_vars = {(x, y): model.continuous_var(
                               name="pixel_{}_{}".format(x, y), lb=0.0, ub=1.0)
                      for y in range(cascade.height)
                      for x in range(cascade.width)}
        feature_vars = {idx: model.binary_var(name="feature_{}".format(idx))
                        for idx in range(len(cascade.features))}

        for stage in cascade.stages:
            # If the classifier's pass value is greater than its fail value,
            # then add a constraint equivalent to the following:
            #   
            #   feature var set => corresponding feature is present in image
            #
            # This is sufficient because if a feature is present, but the
            # corresponding feature var is not set, then setting the feature
            # var will only help the stage constraint pass (due to the feature
            # var appearing with a positive coefficient there).
            #   
            # Conversely, if the classifier's pass vlaue is less than its fail
            # value, add a constraint equivalent to:
            #
            #   corresponding feature is present in image => feature var set
            for classifier in stage.weak_classifiers:
                feature_array = cascade.feature_to_array(
                                                        classifier.feature_idx)
                feature_array /= (cascade.width * cascade.height)
                if classifier.pass_val >= classifier.fail_val:
                    adjusted_classifier_threshold = (classifier.threshold +
                                                                       epsilon)
                    model.add_constraint(sum(pixel_vars[x, y] *
                                             feature_array[y, x]
                                         for y in range(cascade.height)
                                         for x in range(cascade.width)
                                         if feature_array[y, x] != 0.) -
                        adjusted_classifier_threshold *
                        feature_vars[classifier.feature_idx] >= 0)
                else:
                    adjusted_classifier_threshold = (classifier.threshold -
                                                                       epsilon)
                    model.add_constraint(sum(pixel_vars[x, y] *
                                             feature_array[y, x]
                                           for y in range(cascade.height)
                                           for x in range(cascade.width)
                                           if feature_array[y, x] != 0.) +
                        adjusted_classifier_threshold *
                        feature_vars[classifier.feature_idx] <=
                                                 adjusted_classifier_threshold)

            # Enforce that the sum of features present in this stage exceeds
            # the stage threshold.
            fail_val_total = sum(c.fail_val for c in stage.weak_classifiers)
            adjusted_stage_threshold = stage.threshold + epsilon
            model.add_constraint(sum((c.pass_val - c.fail_val) *
                                                    feature_vars[c.feature_idx] 
                         for c in stage.weak_classifiers) >=
                                     adjusted_stage_threshold - fail_val_total)

        self.cascade = cascade
        self.pixel_vars = pixel_vars
        self.feature_vars = feature_vars
        self.model = model


def test_cascade_detect(im, cascade_file):
    my_cascade = Cascade.load(cascade_file)

    im = im[:]
    if len(im.shape) == 3:
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    else:
        assert len(im.shape) == 2
        gray = im

    opencv_cascade = cv2.CascadeClassifier(cascade_file)
    objs = opencv_cascade.detectMultiScale(gray, 1.3, 5)
    for idx, (x, y, w, h) in enumerate(objs):
        cv2.rectangle(im, (x, y), (x + w, y + h), (255, 0, 0), 2)
        cv2.imwrite('out{:02d}.jpg'.format(idx), im)

        assert my_cascade.detect(gray[y:(y + h), x:(x + w)]) == 1


def find_min_face(cascade_file):
    cascade = Cascade.load(cascade_file)
    
    cascade_model = CascadeModel(cascade)
    cascade_model.model.set_objective("min",
                             sum(v for v in cascade_model.pixel_vars.values()))

    cascade_model.model.print_information()
    cascade_model.model.export_as_lp(basename='docplex_%s', path='/home/matt')

    if not cascade_model.model.solve():
        raise Exception("Failed to find solution")
    cascade_model.model.report()

    sol = numpy.array([[cascade_model.pixel_vars[x, y].solution_value
                        for x in range(cascade.width)]
                       for y in range(cascade.height)])

    return sol
    

#test_cascade_detect(cv2.imread(sys.argv[1]), sys.argv[2])
im = find_min_face(sys.argv[1])
im *= 256.
im_resized = cv2.resize(im, (im.shape[1] * 10, im.shape[0] * 10),
                        interpolation=cv2.INTER_NEAREST)
cv2.imwrite("out.png", im_resized)

cascade = Cascade.load(sys.argv[1])
assert cascade.detect(im) == 1

