"""
geuvadis_metadeta.py

Reads https://github.com/alyssafrazee/ballgown_code/blob/master/
GEUVADIS_preprocessing/pop_data_annot_whole.txt
and a CSV version of
ftp://ftp.1000genomes.ebi.ac.uk/vol1/ftp/technical/working/
20130606_sample_info/20130606_sample_info.xlsx
to determine sample metadata for all of GEUVADIS manifest
(GEUVADIS_all_samples.manifest). Outputs random sample of paired-end
GEUVADIS samples -- comment lines provide metadata.

Make the eval directory the current working directory before executing.

Default values of command-line parameters were used for Rail simulation.
"""
import argparse
import sys
import random

# Print file's docstring if -h is invoked
parser = argparse.ArgumentParser(description=__doc__, 
            formatter_class=argparse.RawDescriptionHelpFormatter)
# Add command-line arguments
parser.add_argument('--samples', type=int,
        default=100,
        help='Number of samples to grab from GEUVADIS at random'
    )
parser.add_argument('--seed', type=int,
        default=0,
        help='Random seed to use'
    )

args = parser.parse_args()

fastq_to_sample = {}
sample_to_metadata = {}

with open('pop_data_annot_whole.txt') as fastq_to_sample_stream:
    for line in fastq_to_sample_stream:
        tokens = line.strip().split('\t')
        fastq_to_sample[tokens[0]] = tokens[1].partition('_')[0]

with open('20130606_sample_info/Sample Info-Table 1.csv') \
    as sample_to_metadata_stream:
    line = sample_to_metadata_stream.readline()
    line = sample_to_metadata_stream.readline()
    while line:
        tokens = line.strip().split(',')
        if 'female' in tokens:
            sex = 'female'
        else:
            assert 'male' in tokens
            sex = 'male'
        sample_to_metadata[tokens[0]] = (sex, tokens[2]) #sex, hapmap pop
        line = sample_to_metadata_stream.readline()

manifest_line_to_metadata = {}

with open('GEUVADIS_all_samples.manifest') as geuvadis_stream:
    for line in geuvadis_stream:
        if line[0] == '#' or not line.strip(): # comment line
            continue
        tokens = line.strip().split('\t')
        if len(tokens) != 5: continue # paired-end lines only
        fastq_name = tokens[0].rpartition('/')[-1].partition('_')[0]
        manifest_line_to_metadata[line] = (fastq_to_sample[fastq_name],) + \
            sample_to_metadata[fastq_to_sample[fastq_name]]

random.seed(args.seed)
lines = random.sample(manifest_line_to_metadata.keys(), args.samples)
with open('GEUVADIS_100_samples.manifest', 'w') as geuvadis_stream:
    print >>geuvadis_stream, '# 100 random samples of all of GEUVADIS'
    print >>geuvadis_stream, '# Generated by geuvadis_metadata.py from: '
    print >>geuvadis_stream,  ('# 1. https://github.com/alyssafrazee/'
           'ballgown_code/blob/master/'
           'GEUVADIS_preprocessing/pop_data_annot_whole.txt, which associates '
           'fastq identifiers with 1000 Genomes sample names')
    print >>geuvadis_stream, ('# 2. ftp://ftp.1000genomes.ebi.ac.uk/vol1/ftp/'
           'technical/working/'
           '20130606_sample_info/20130606_sample_info.xlsx, which provides '
           'metadata on 1000 Genomes samples')
    print >>geuvadis_stream, ('# 3. GEUVADIS_all_samples.manifest, a '
           'Myrna-style manifest file listing all GEUVADIS samples.\n')
    for line in lines:
        print >>geuvadis_stream, '# %s,%s,%s' % manifest_line_to_metadata[line]
        geuvadis_stream.write(line)