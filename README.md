This is the source repository for the scalable suite RNA-seq analysis
software that has yet to be named.

Design
------

![Design flowchart](doc/design.pdf "Design flowchart")

Repository layout
-----------------

* `src`: All sources, including Python, R and C
* `data`: Manifest files describing big datasets and some scripts for running
  them through this pipeline as well as Myrna
* `doc`: Documentation
* `emr`: Scripts related to EMR mode
* `example`: Some examples of how to run the pipeline
* `hadoop`: Source related to the modes that use Hadoop
* `lib`: Required Java libraries
* `old`: Deprecated code from pre-Fall-2013
* `package`: Scripts for packaging up the tool and reference archives
* `tools`: Scripts for building some helpful tools

TODO list
---------

* Pick a name!  Or a temporary codename.  Replace old name(s) like Myrna 2,
  Tornado and RNAwesome, with new name.  Make it easier to change name in
  future by minimizing number of places where it's hard-coded.
* Carefully audit the code in `src/rnawesome/align.py` and add a comprehensive
  suite of unit tests.
* Move this TODO list to GitHub issue tracker

Running the drosophila example
------------------------------

Tool prerequisites:
* [Python]
* [SWIG]
* [Bowtie]
* [bedToBigBed]

[Python]: http://www.python.org
[SWIG]: http://www.swig.org
[Bowtie]: http://bowtie-bio.sourceforge.net/index.shtml
[bedToBigBed]: http://hgdownload.cse.ucsc.edu/admin/exe/

Data prerequisites:
* Expanded dm3 [iGenomes] tarball somewhere on your machine

[iGenomes]: http://support.illumina.com/sequencing/sequencing_software/igenome.ilmn

Configuration:
* Set `TORNADO_HOME` environment variable to point to base of this checkout
* Set `IGENOMES_HOME` environment variable to point to directory that has
  `Drosophila_melanogaster` from iGenomes tarball as a subdirectory

To prepare for the test:

    cd $TORNADO_HOME
    make -C src
    cd example/dmel_flux
    make
    (alternately, do "make -jN" where N = number of cores you can spare)

That last make command will take a while.  To run the test do the following
from `$TORNADO_HOME/example/dmel_flux`):

    sh local.sh
