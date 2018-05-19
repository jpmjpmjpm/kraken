# -*- coding: utf-8 -*-
#
# Copyright 2015 Benjamin Kiessling
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""
kraken.rpred
~~~~~~~~~~~~

Generators for recognition on lines images.
"""
from __future__ import absolute_import, division, print_function

import numpy as np
import bidi.algorithm as bd

from PIL import Image

from kraken.lib.util import pil2array, array2pil
from kraken.lib.exceptions import KrakenInputException
from kraken.lib.dataset import generate_input_transforms


__all__ = ['ocr_record', 'bidi_record', 'mm_rpred', 'rpred']


class ocr_record(object):
    """
    A record object containing the recognition result of a single line
    """
    def __init__(self, prediction, cuts, confidences):
        self.prediction = prediction
        self.cuts = cuts
        self.confidences = confidences

    def __len__(self):
        return len(self.prediction)

    def __str__(self):
        return self.prediction

    def __iter__(self):
        self.idx = -1
        return self

    def __next__(self):
        if self.idx + 1 < len(self):
            self.idx += 1
            return (self.prediction[self.idx], self.cuts[self.idx],
                    self.confidences[self.idx])
        else:
            raise StopIteration

    def __getitem__(self, key):
        if isinstance(key, slice):
            return [self[i] for i in range(*key.indices(len(self)))]
        elif isinstance(key, int):
            if key < 0:
                key += len(self)
            if key >= len(self):
                raise IndexError('Index (%d) is out of range' % key)
            return (self.prediction[key], self.cuts[key],
                    self.confidences[key])
        else:
            raise TypeError('Invalid argument type')


def bidi_record(record):
    """
    Reorders a record using the Unicode BiDi algorithm.

    Models trained for RTL or mixed scripts still emit classes in LTR order
    requiring reordering for proper display.

    Args:
        record (kraken.rpred.ocr_record)

    Returns:
        kraken.rpred.ocr_record
    """
    storage = bd.get_empty_storage()
    base_level = bd.get_base_level(record.prediction)
    storage['base_level'] = base_level
    storage['base_dir'] = ('L', 'R')[base_level]

    bd.get_embedding_levels(record.prediction, storage)
    bd.explicit_embed_and_overrides(storage)
    bd.resolve_weak_types(storage)
    bd.resolve_neutral_types(storage, False)
    bd.resolve_implicit_levels(storage, False)
    for i, j in enumerate(record):
        storage['chars'][i]['record'] = j
    bd.reorder_resolved_levels(storage, False)
    bd.apply_mirroring(storage, False)
    prediction = u''
    cuts = []
    confidences = []
    for ch in storage['chars']:
        prediction = prediction + ch['record'][0]
        cuts.append(ch['record'][1])
        confidences.append(ch['record'][2])
    return ocr_record(prediction, cuts, confidences)


def extract_boxes(im, bounds):
    """
    Yields the subimages of image im defined in the list of bounding boxes in
    bounds preserving order.

    Args:
        im (PIL.Image): Input image
        bounds (list): A list of tuples (x1, y1, x2, y2)

    Yields:
        (PIL.Image) the extracted subimage
    """
    if bounds['text_direction'].startswith('vertical'):
        angle = 90
    else:
        angle = 0
    for box in bounds['boxes']:
        if isinstance(box, tuple):
            box = list(box)
        if (box < [0, 0, 0, 0] or box[::2] > [im.size[0], im.size[0]] or
                box[1::2] > [im.size[1], im.size[1]]):
            raise KrakenInputException('Line outside of image bounds')
        yield im.crop(box).rotate(angle, expand=True), box


def mm_rpred(nets, im, bounds, pad=16, bidi_reordering=True):
    """
    Multi-model version of kraken.rpred.rpred.

    Takes a dictionary of ISO15924 script identifiers->models and an
    script-annotated segmentation to dynamically select appropriate models for
    these lines.

    Args:
        nets (dict): A dict mapping ISO15924 identifiers to TorchSegRecognizer
                     objects. Recommended to be an defaultdict.
        im (PIL.Image): Image to extract text from
                        bounds (dict): A dictionary containing a 'boxes' entry
                        with a list of lists of coordinates (script, (x0, y0,
                        x1, y1)) of a text line in the image and an entry
                        'text_direction' containing
                        'horizontal-tb/vertical-lr/rl'.
        pad (int): Extra blank padding to the left and right of text line
        bidi_reordering (bool): Reorder classes in the ocr_record according to
                                the Unicode bidirectional algorithm for correct
                                display.
    Yields:
        An ocr_record containing the recognized text, absolute character
        positions, and confidence values for each character.

    Raises:
        KrakenInputException if the mapping between segmentation scripts and
        networks is incomplete.
    """
    miss = [x[0] for x in bounds['boxes'] if not nets.get(x[0])]
    if miss:
        raise KrakenInputException('Missing models for scripts {}'.format(miss))

    # build dictionary for line preprocessing
    ts = {}
    for script, network in nets.iteritems():
        batch, channels, height, width = network.nn.input
        ts[script] = generate_input_transforms(batch, height, width, channels, pad)

    for line in bounds['boxes']:
        rec = ocr_record('', [], [])
        for script, (box, coords) in zip(map(lambda x: x[0], line),
                                         extract_boxes(im, {'text_direction': bounds['text_direction'],
                                                            'boxes': map(lambda x: x[1], line)})):
            # check if boxes are non-zero in any dimension
            if sum(coords[::2]) == 0 or coords[3] - coords[1] == 0:
                continue
            # try conversion into tensor
            try:
                line = ts[script](box)
            except Exception:
                yield ocr_record('', [], [])
                continue
            # check if line is non-zero
            if line.max() == line.min():
                yield ocr_record('', [], [])
                continue
            preds = nets[script].predict(line)

            # calculate recognized LSTM locations of characters
            scale = box.size[0]/(len(nets[script].outputs)-2 * pad)
            pred = ''.join(x[0] for x in preds)
            pos = []
            conf = []

            for _, start, end, c in preds:
                if bounds['text_direction'].startswith('horizontal'):
                    pos.append((coords[0] + int((start-pad)*scale), coords[1], coords[0] + int((end-pad)*scale), coords[3]))
                else:
                    pos.append((coords[0], coords[1] + int((start-pad)*scale), coords[2], coords[1] + int((end-pad)*scale)))
                conf.append(c)
            rec.prediction += pred
            rec.cuts.extend(pos)
            rec.confidences.extend(conf)
        if bidi_reordering:
            yield bidi_record(rec)
        else:
            yield rec


def rpred(network, im, bounds, pad=16, bidi_reordering=True):
    """
    Uses a RNN to recognize text

    Args:
        network (kraken.lib.models.TorchSeqRecognizer): A TorchSegRecognizer
                                                        object
        im (PIL.Image): Image to extract text from
        bounds (dict): A dictionary containing a 'boxes' entry with a list of
                       coordinates (x0, y0, x1, y1) of a text line in the image
                       and an entry 'text_direction' containing
                       'horizontal-tb/vertical-lr/rl'.
        pad (int): Extra blank padding to the left and right of text line.
                   Auto-disabled when expected network inputs are incompatible
                   with padding.
        bidi_reordering (bool): Reorder classes in the ocr_record according to
                                the Unicode bidirectional algorithm for correct
                                display.
    Yields:
        An ocr_record containing the recognized text, absolute character
        positions, and confidence values for each character.
    """
    batch, channels, height, width = network.nn.input
    ts = generate_input_transforms(batch, height, width, channels, pad)

    for box, coords in extract_boxes(im, bounds):
        # check if boxes are non-zero in any dimension
        if sum(coords[::2]) == 0 or coords[3] - coords[1] == 0:
            yield ocr_record('', [], [])
            continue
        # try conversion into tensor
        try:
            line = ts(box)
        except Exception:
            yield ocr_record('', [], [])
            continue
        # check if line is non-zero
        if line.max() == line.min():
            yield ocr_record('', [], [])
            continue
        preds = network.predict(line)
        # calculate recognized LSTM locations of characters
        scale = box.size[0]/(len(network.outputs)-2 * pad)
        # XXX: fix bounding box calculation ocr_record for multi-codepoint labels.
        pred = ''.join(x[0] for x in preds)
        pos = []
        conf = []

        for _, start, end, c in preds:
            if bounds['text_direction'].startswith('horizontal'):
                pos.append((coords[0] + int((start-pad)*scale), coords[1], coords[0] + int((end-pad)*scale), coords[3]))
            else:
                pos.append((coords[0], coords[1] + int((start-pad)*scale), coords[2], coords[1] + int((end-pad)*scale)))
            conf.append(c)
        if bidi_reordering:
            yield bidi_record(ocr_record(pred, pos, conf))
        else:
            yield ocr_record(pred, pos, conf)
