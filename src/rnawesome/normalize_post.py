'''
normalize_post.py

Takes results from the normalize phase as a single bin and writes out a
new manifest file annotated with normalization factors. 
'''

import os
import sys
import site
import argparse

base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
site.addsitedir(os.path.join(base_path, "manifest"))

import manifest

parser = argparse.ArgumentParser(description=\
    'Take results from normalization phase and write them to a file.')

parser.add_argument(\
    '--out', metavar='PATH', type=str, required=True,
    help='File to write output to')

manifest.addArgs(parser)
args = parser.parse_args()

# Get the set of all labels by parsing the manifest file, given on the
# filesystem or in the Hadoop file cache
labs = manifest.labels(args)
ls = sorted(labs)

ninp = 0       # # lines input so far
facts = dict() # Normalization factors

for ln in sys.stdin:
    ln = ln.rstrip()
    toks = ln.split('\t')
    assert len(toks) == 2
    facts[toks[0]] = int(toks[1])
    ninp += 1

ofh = open(args.out, 'w')
for l in ls:
    if l in facts: ofh.write("%s\t%d\n" % (l, facts[l]))
    else: ofh.write("%s\tNA\n" % l)
ofh.close()

# Done
print >>sys.stderr, "DONE with normalize_post.py; in = %d" % ninp