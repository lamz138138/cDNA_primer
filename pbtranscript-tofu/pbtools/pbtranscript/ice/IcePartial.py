#!/usr/bin/env python
###########################################################################
# Copyright (c) 2011-2014, Pacific Biosciences of California, Inc.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted (subject to the limitations in the
# disclaimer below) provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright
#  notice, this list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above
#  copyright notice, this list of conditions and the following
#  disclaimer in the documentation and/or other materials provided
#  with the distribution.
#
#  * Neither the name of Pacific Biosciences nor the names of its
#  contributors may be used to endorse or promote products derived
#  from this software without specific prior written permission.
#
# NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE
# GRANTED BY THIS LICENSE. THIS SOFTWARE IS PROVIDED BY PACIFIC
# BIOSCIENCES AND ITS CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
# OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL PACIFIC BIOSCIENCES OR ITS
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
# USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
# OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
###########################################################################
"""
Given an input_fasta file of non-full-length (partial) reads and
(unpolished) consensus isoforms sequences in ref_fasta, align reads to
consensus isoforms using BLASR, and then build up a mapping between
consensus isoforms and reads (i.e., assign reads to isoforms).
Finally, save
    {isoform_id: [read_ids],
     nohit: set(no_hit_read_ids)}
to an output pickle file.
"""

import os
import os.path as op
import logging
import time
from cPickle import dump
from pbcore.io import FastaReader
from pbcore.util.Process import backticks
from pbtools.pbtranscript.Utils import realpath, touch, real_upath
from pbtools.pbtranscript.PBTranscriptOptions import add_fofn_arguments
from pbtools.pbtranscript.ice.ProbModel import ProbFromModel, ProbFromQV, ProbFromFastq
from pbtools.pbtranscript.ice.IceUtils import blasr_against_ref
from pbtools.pbtranscript.ice.IceUtils import ice_fa2fq

def build_uc_from_partial(input_fasta, ref_fasta, out_pickle,
                          sa_file=None, ccs_fofn=None,
                          done_filename=None, blasr_nproc=12, use_finer_qv=False):
    """
    Given an input_fasta file of non-full-length (partial) reads and
    (unpolished) consensus isoforms sequences in ref_fasta, align reads to
    consensus isoforms using BLASR, and then build up a mapping between
    consensus isoforms and reads (i.e., assign reads to isoforms).
    Finally, save
        {isoform_id: [read_ids],
         nohit: set(no_hit_read_ids)}
    to an output pickle file.

    ccs_fofn --- If None, assume no quality value is available,
    otherwise, use QV from ccs_fofn.
    blasr_nproc --- equivalent to blasr -nproc, number of CPUs to use
    """
    input_fasta = realpath(input_fasta)
    m5_file = input_fasta + ".blasr"
    out_pickle = realpath(out_pickle)
    if sa_file is None:
        if op.exists(input_fasta + ".sa"):
            sa_file = input_fasta + ".sa"

    cmd = "blasr {i} ".format(i=real_upath(input_fasta)) + \
          "{r} -bestn 5 ".format(r=real_upath(ref_fasta)) + \
          "-nproc {n} -m 5 ".format(n=blasr_nproc) + \
          "-maxScore -1000 -minPctIdentity 85 " + \
          "-out {o} ".format(o=real_upath(m5_file))
    if sa_file is not None and op.exists(sa_file):
        cmd += "-sa {sa}".format(sa=real_upath(sa_file))

    logging.info("CMD: {cmd}".format(cmd=cmd))
    _out, _code, _msg = backticks(cmd)
    if _code != 0:
        errMsg = "Command failed: {cmd}\n{e}".format(cmd=cmd, e=_msg)
        logging.error(errMsg)
        raise RuntimeError(errMsg)
    
    if ccs_fofn is None:
        logging.info("Loading probability from model (0.01,0.07,0.06)")
        probqv = ProbFromModel(.01, .07, .06)
    else:
        start_t = time.time()
        if use_finer_qv:
            logging.info("Loading QVs from {i} + {f} took {s} secs".format(f=ccs_fofn, i=input_fasta,\
                    s=time.time()-start_t))
            probqv = ProbFromQV(input_fofn=ccs_fofn, fasta_filename=input_fasta)
        else:
            input_fastq = input_fasta[:input_fasta.rfind('.')] + '.fastq'
            logging.info("Converting {i} + {f} --> {fq}".format(i=input_fasta, f=ccs_fofn, fq=input_fastq))
            ice_fa2fq(input_fasta, ccs_fofn, input_fastq)
            logging.info("Loading QVs from {fq} took {s} secs".format(fq=input_fastq, s=time.time()-start_t))
            probqv = ProbFromFastq(input_fastq)


    logging.info("Calling blasr_against_ref ...")
    hitItems = blasr_against_ref(output_filename=m5_file,
                                 is_FL=False,
                                 sID_starts_with_c=True,
                                 qver_get_func=probqv.get_smoothed,
                                 ece_penalty=1,
                                 ece_min_len=10,
                                 same_strand_only=False)

    partial_uc = {}  # Maps each isoform (cluster) id to a list of reads
    # which can map to the isoform
    seen = set()  # reads seen
    logging.info("Building uc from BLASR hits.")
    for h in hitItems:
        if h.ece_arr is not None:
            if h.cID not in partial_uc:
                partial_uc[h.cID] = []
            partial_uc[h.cID].append(h.qID)
            seen.add(h.qID)

    allhits = set(r.name.split()[0] for r in FastaReader(input_fasta))

    logging.info("Counting reads with no hit.")
    nohit = allhits.difference(seen)

    logging.info("Dumping uc to a pickle: {f}.".format(f=out_pickle))
    with open(out_pickle, 'w') as f:
        dump({'partial_uc': partial_uc, 'nohit': nohit}, f)

    os.remove(m5_file)

    done_filename = realpath(done_filename) if done_filename is not None \
        else out_pickle + '.DONE'
    logging.debug("Creating {f}.".format(f=done_filename))
    touch(done_filename)


class IcePartialOne(object):

    """Assign nfl reads of a given fasta to isoforms."""

    desc = "Assign non-full-length reads in the given input fasta to " + \
           "unpolished consensus isoforms."
    prog = "ice_partial.py one "

    def __init__(self, input_fasta, ref_fasta, out_pickle,
                 sa_file=None, ccs_fofn=None,
                 done_filename=None, blasr_nproc=12, use_finer_qv=False):
        self.input_fasta = input_fasta
        self.ref_fasta = ref_fasta
        self.out_pickle = out_pickle
        self.sa_file = sa_file
        self.ccs_fofn = ccs_fofn
        self.done_filename = done_filename
        self.blasr_nproc = blasr_nproc
        self.use_finer_qv = use_finer_qv

    def cmd_str(self):
        """Return a cmd string (ice_partial.py one)."""
        return self._cmd_str(input_fasta=self.input_fasta,
                             ref_fasta=self.ref_fasta,
                             out_pickle=self.out_pickle,
                             sa_file=self.sa_file,
                             ccs_fofn=self.ccs_fofn,
                             done_filename=self.done_filename,
                             blasr_nproc=self.blasr_nproc)

    def _cmd_str(self, input_fasta, ref_fasta, out_pickle,
                 sa_file=None, ccs_fofn=None,
                 done_filename=None, blasr_nproc=12):
        """Return a cmd string (ice_partil.py one)"""
        cmd = self.prog + \
              "{f} ".format(f=input_fasta) + \
              "{r} ".format(r=ref_fasta) + \
              "{o} ".format(o=out_pickle)
        if sa_file is not None:
            cmd += "--sa {s} ".format(s=sa_file)
        if ccs_fofn is not None:
            cmd += "--ccs_fofn {c} ".format(c=ccs_fofn)
        if done_filename is not None:
            cmd += "--done {d} ".format(d=done_filename)
        if blasr_nproc is not None:
            cmd += "--blasr_nproc {b} ".format(b=blasr_nproc)
        return cmd

    def run(self):
        """Run"""
        logging.info("Building uc from non-full-length reads.")
        build_uc_from_partial(input_fasta=self.input_fasta,
                              ref_fasta=self.ref_fasta,
                              out_pickle=self.out_pickle,
                              sa_file=self.sa_file,
                              ccs_fofn=self.ccs_fofn,
                              blasr_nproc=self.blasr_nproc,
                              use_finer_qv=self.use_finer_qv)


def add_ice_partial_one_arguments(parser):
    """Add arguments for assigning nfl reads of a given input fasta
    to unpolished isoforms."""
    parser.add_argument("input_fasta", help="Input fasta split file")
    parser.add_argument("ref_fasta",
                        help="Reference fasta file, most likely " +
                             "ref_consensus.fa from ICE output")
    parser.add_argument("out_pickle", type=str, help="Output pickle files.")
    parser = add_fofn_arguments(parser, ccs_fofn=True)
    parser.add_argument("--sa", dest="sa_file", default=None,
                        help="Suffix array of ref_fasta")
    parser.add_argument("--done", dest="done_filename", type=str,
                        help="An empty file generated to indicate that " +
                             "out_pickle is done.")
    parser.add_argument("--blasr_nproc", dest="blasr_nproc",
                        type=int, default=12,
                        help="blasr -nproc, number of CPUs [default: 12]")
    parser.add_argument("--use_finer_qv", action="store_true", default=False,
                        help="Use finer QV which uses more memory & time [default: False]")
    return parser


#import sys
#from pbcore.util.ToolRunner import PBToolRunner
#from pbtools.pbtranscript.__init__ import get_version
#
# class IcePartialOneRunner(PBToolRunner):
#    """IcePartial Runner"""
#    def __init__(self):
#        PBToolRunner.__init__(self, IcePartialOne.desc)
#        add_ice_partial_one_arguments(self.parser)
#
#    def getVersion(self):
#        """Get version string."""
#        return get_version()
#
#    def run(self):
#        """Run"""
#        logging.info("Running {f} v{v}.".format(f=op.basename(__file__),
#                                                v=self.getVersion()))
#
#        args = self.args
#        logging.info("Building uc from non-full-length reads.")
#        build_uc_from_partial(input_fasta=args.input_fasta,
#                              ref_fasta=args.ref_fasta,
#                              out_pickle=args.out_pickle,
#                              sa_file=args.sa_file,
#                              ccs_fofn=args.ccs_fofn,
#                              blasr_nproc=args.blasr_nproc)
#        return 0
#
#
# def main():
#    """Main function"""
#    runner = IcePartialOneRunner()
#    return runner.start()
#
# if __name__ == "__main__":
#    sys.exit(main())
