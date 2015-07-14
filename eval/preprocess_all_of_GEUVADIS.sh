#!/usr/bin/env bash
# Use Rail-RNA v0.1.8 to reproduce results in v2 of Rail paper

# Set output directory here -- must be on S3!
OUTPUT=s3://rail-eu-west-1/geuv667prepped

# Change --ec2-key-name as appropriate below or simply delete it if you don't intend to SSH to the EMR cluster
rail-rna prep elastic -m GEUVADIS_all_descriptive_revised.manifest -c 20 -o $OUTPUT --core-instance-bid-price 0.13 --master-instance-bid-price 0.13 -f --region eu-west-1 --do-not-check-manifest --ec2-key-name raileuw1