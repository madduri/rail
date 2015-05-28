#!/usr/bin/env python
"""
guess.py
Part of Rail-RNA

Tools for inferring the properties of samples.
"""
from itertools import islice
import math
import random
import sys

'''Ranges of possible quality values.
Sanger : (33, 93)
Solexa : (59, 104)
Phred64 : (64, 104) . Check out maxes and mins of ord(chars) in
phred_converter functions below; they are written to guarantee that the ranges
of quality chars fall within these ranges of valid chars.'''

def inferred_phred_format(fastq_stream, sample_size=10000, allowed_fails=5,
                            verbose=True):
    """ Studies a selection of reads from a sample to determine Phred format.

        fastq_stream: where to read input fastq lines or None if format is
            provided
        sample_size: number of quality records to sample from file
        allowed_fails: number of failed records before giving up
        verbose: talk about range of quality values found in FASTQ

        Return value: one of {Sanger, Solexa, Phred64}; assumes Sanger if no
            distinguishing characters are found or if it's a FASTA file
    """
    first_line = fastq_stream.readline()
    try:
        if first_line[0] in ['>', ';']:
            # It's a FASTA file; return Sanger immediately
            return 'Sanger'
    except IndexError:
        return 'Sanger'
    # Now assume FASTQ; use first line as seed
    random.seed(first_line)
    quals = []
    # Grab random sample of sample_size quals
    for i, qual in enumerate(islice(fastq_stream, 2, None, 4)):
        if len(quals) < sample_size:
            quals.append(qual.strip())
        elif random.random() * (i + 1) < sample_size:
            quals[random.randint(0, sample_size - 1)] = qual.strip()
    # Get range of quality scores
    quals = [ord(char) for char in set(''.join(quals))]
    try:
        qual_range = (min(quals), max(quals))
    except ValueError:
        # Empty list; default to Sanger
        return 'Sanger'
    if verbose:
        print >>sys.stderr, (
                'Range of quality values found from random sample of {} '
                'records is ({}, {}).'
            ).format(sample_size, qual_range[0], qual_range[1])
    if qual_range[0] >= 64:
        # Don't even check max; choose Phred64 and round down as necessary
        return 'Phred64'
    if qual_range[0] < 59:
        '''Now we're choosing between Sanger and Solexa, and this means there
        are Sanger-unique characters.'''
        return 'Sanger'
    # Min qual is now on [59, 63]; could still be either Sanger or Solexa
    if qual_range[1] >= 94:
        return 'Solexa'
    # Default to Sanger
    return 'Sanger'

def phred_converter(fastq_stream=None, phred_format=None, at_once=500):
    """ Provides a function that converts a quality string to Sanger format

        Inspired by https://github.com/brentp/bio-playground/blob/master/
        reads-utils/guess-encoding.py . Returns a function

        fastq_stream: where to read input fastq lines or None if format is
            provided
        phred_format: Phred format from _RANGES or None if fastq_stream is
            provided
        at_once: number of quality records to read in at once before checking
            for distinguishing characters

        Return value: one of {Sanger, Solexa, Phred64}
    """
    assert fastq_stream is not None or phred_format is not None, (
        'Either a fastq stream must be provided to infer phred format '
        'or a phred_format must be provided directly.')
    assert phred_format is None or phred_format in _RANGES, (
            'Phred format must be Sanger or Phred64'
        )
    if phred_format is None:
        phred_format = inferred_phred_format(fastq_stream, at_once)
    if phred_format == 'Solexa':
       def final_converter(qual):
           return ''.join([
                               chr(round(
                           10*math.log(1+10**((min(max(ord(char), 59), 104)-64)
                            /10.0),10)
                   )+33) for char in qual
                ])
    elif phred_format == 'Sanger':
        def final_converter(qual):
            return ''.join(chr(min(max(ord(char), 33), 93)) for char in qual)
    else:
        assert phred_format == 'Phred64'
        # It's Phred64
        def final_converter(qual):
            return ''.join(chr(min(max(ord(char), 64), 104) - 31)
                                for char in qual)
    return final_converter