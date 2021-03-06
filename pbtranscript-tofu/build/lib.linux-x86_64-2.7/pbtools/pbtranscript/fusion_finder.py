#!/usr/bin/env python
import os, sys
import itertools
from cPickle import *
from collections import defaultdict, namedtuple
from pbtools.pbtranscript.Utils import check_ids_unique
import pbtools.pbtranscript.tofu_wrap as tofu_wrap
import pbtools.pbtranscript.BioReaders as BioReaders
import pbtools.pbtranscript.branch.branch_simple2 as branch_simple2
import pbtools.pbtranscript.counting.compare_junctions as compare_junctions

from bx.intervals.cluster import ClusterTree

def sep_by_strand(records):
    output = {'+':[], '-':[]}
    for r in records:
        output[r.flag.strand].append(r)
    return output

def is_fusion_compatible(r1, r2, max_fusion_point_dist, allow_extra_5_exons):
    """
    Helper function for: merge_fusion_exons()

    Check that:
    (1) r1, r2 and both in the 5', or both in the 3'
    (2) if single-exon, fusion point must be close by
        if multi-exon, every junction identical (plus below is True)
    (3) if allow_extra_5_exons is False, num exons must be the same
        if allow_extra_5_exons is True, only allow additional 5' exons
    """
    MAX_QSTART_FOR_5 = 100
    # first need to figure out ends
    # also check that both are in the 5' portion of r1 and r2
    assert r1.flag.strand == r2.flag.strand
    if r1.qStart < MAX_QSTART_FOR_5: # in the 5' portion of r1
        if r2.qStart > MAX_QSTART_FOR_5: # in the 3' portion, reject
            return False
        in_5_portion = True
    else: # in the 3' portion of r1
        if r2.qStart < MAX_QSTART_FOR_5: 
            return False
        in_5_portion = False
    plus_is_5end = (r1.flag.strand == '+')

    type = compare_junctions.compare_junctions(r1, r2)
    if type == 'exact':
        if len(r1.segments) == 1:
            if len(r2.segments) == 1:
                # single exon case, check fusion point is close enough
                if in_5_portion and plus_is_5end: dist = abs(r1.sStart - r2.sStart)
                else: dist = abs(r1.sEnd - r2.sEnd)
                return dist <= max_fusion_point_dist
            else:
                raise Exception, "Not possible case for multi-exon transcript and " + \
                        "single-exon transcript to be exact!"
        else: # multi-exon case, must be OK
            return True
    elif type == 'super' or type == 'subset':
        if allow_extra_5_exons:
            # check that the 3' junction is identical
            # also check that the 3' end is relatively close
            if in_5_portion and plus_is_5end:
                if r1.segments[-1].start != r2.segments[-1].start: return False
                if abs(r1.segments[-1].end - r2.segments[-1].end) > max_fusion_point_dist: return False
            elif in_5_portion and (not plus_is_5end):
                if r1.segments[0].end != r2.segments[0].end: return False
                if abs(r1.segments[0].start - r2.segments[0].start) > max_fusion_point_dist: return False
            else:
                return False
        else: # not OK because number of exons must be the same
            return False
    else: #ex: partial, nomatch, etc...
        return False

def merge_fusion_exons(records, max_fusion_point_dist, allow_extra_5_exons):
    """
    Records is a list of overlapping GMAP SAM Records (must be on same strand)
    Unlike regular (non-fusion) mapping, only merge records if:

    (1) for multi-exon, every junction is identical
        for single-exon, the fusion point is no bigger than <max_fusion_point_dist> apart

    (2) if allow_extra_5_exons is False, number of exons must be the same
        if allow_extra_5_exons is True, only merge if the extension is in the 5' direction

    Returns a list of grouped records, ex: [[r1,r2], [r3], [r4, r5, r6]]....
    which can be sent to BranchSimple.process_records for writing out
    """
    output = [[records[0]]]
    for r1 in records[1:]:
        merged = False
        # go through output, seeing if mergeable
        for i, r2s in enumerate(output):
            if all(is_fusion_compatible(r1, r2, max_fusion_point_dist, allow_extra_5_exons) for r2 in r2s):
                output[i].append(r1)
                merged = True
                break
        if not merged:
            output.append([r1])
    return output

def iter_gmap_sam_for_fusion(gmap_sam_filename, fusion_candidates, transfrag_len_dict):
    """
    Iterate through a sorted GMAP SAM file
    Continuously yield a group of overlapping records {'+': [r1, r2, ...], '-': [r3, r4....]}
    """
    records = []
    iter = BioReaders.GMAPSAMReader(gmap_sam_filename, True, query_len_dict=transfrag_len_dict)
    for r in iter:
        if r.qID in fusion_candidates: 
            records = [r]
            break

    for r in iter:
        if len(records) >= 1 and r.sStart < records[-1].sStart:
            print >> sys.stderr, "SAM file is NOT sorted. ABORT!"
            sys.exit(-1)
        if len(records) >= 1 and (r.sID != records[0].sID or r.sStart > records[-1].sEnd):
            yield(sep_by_strand(records))
            records = []
        if r.qID in fusion_candidates:
            records.append(r)

    if len(records) > 0:
        yield(sep_by_strand(records))

def find_fusion_candidates(sam_filename, query_len_dict, min_locus_coverage=.1, min_total_coverage=.99, min_dist_between_loci=100000):
    """
    Return list of fusion candidates qIDs
    (1) must map to 2 or more loci
    (2) minimum coverage for each loci is 10%
    (3) total coverage is >= 99%
    (4) distance between the loci is at least 100kb
    """
    TmpRec = namedtuple('TmpRec', ['qCov', 'qLen', 'qStart', 'qEnd', 'sStart', 'sEnd'])
    def total_coverage(tmprecs):
        tree = ClusterTree(0, 0)
        for r in tmprecs: tree.insert(r.qStart, r.qEnd, -1)
        return sum(reg[1]-reg[0] for reg in tree.getregions())

    d = defaultdict(lambda: [])
    reader = BioReaders.GMAPSAMReader(sam_filename, True, query_len_dict=query_len_dict)
    for r in reader: d[r.qID].append(TmpRec(qCov=r.qCoverage, qLen=r.qLen, qStart=r.qStart, qEnd=r.qEnd, sStart=r.sStart, sEnd=r.sEnd))
    fusion_candidates = []
    for k, data in d.iteritems():
        if len(data) > 1 and \
            all(a.qCov>=min_locus_coverage for a in data) and \
            total_coverage(data)*1./data[0].qLen >= min_total_coverage and \
            all(max(a.sStart,b.sStart)-min(a.sEnd,b.sEnd)>=min_dist_between_loci \
                           for a,b in itertools.combinations(data, 2)):
                    fusion_candidates.append(k)
    return fusion_candidates

def fusion_main(fa_or_fq_filename, sam_filename, output_prefix, is_fq=False, allow_extra_5_exons=True, skip_5_exon_alt=False, prefix_dict_pickle_filename=None):
    """
    (1) identify fusion candidates (based on mapping, total coverage, identity, etc)
    (2) group/merge the fusion exons, using an index to point to each individual part
    (3) use BranchSimple to write out a tmp GFF where 
         PBfusion.1.1 is the first part of a fusion gene
         PBfusion.1.2 is the second part of a fusion gene
    (4) read the tmp file from <3> and modify it so that 
         PBfusion.1 just represents the fusion gene (a single transcript GFF format)
    """
    compressed_records_pointer_dict = defaultdict(lambda: [])
    merged_exons = []
    merged_i = 0
    
    # step (0). check for duplicate IDs
    check_ids_unique(fa_or_fq_filename, is_fq=is_fq)

    # step (1). identify fusion candidates
    bs = branch_simple2.BranchSimple(fa_or_fq_filename, is_fq=is_fq)
    fusion_candidates = find_fusion_candidates(sam_filename, bs.transfrag_len_dict)

    # step (2). merge the fusion exons
    for recs in iter_gmap_sam_for_fusion(sam_filename, fusion_candidates, bs.transfrag_len_dict):
        for v in recs.itervalues():
            if len(v) > 0:
                o = merge_fusion_exons(v, max_fusion_point_dist=100, allow_extra_5_exons=allow_extra_5_exons)
                for group in o:
                    merged_exons.append(group)
                    for r in group: compressed_records_pointer_dict[r.qID].append(merged_i)
                    merged_i += 1

    # step (3). use BranchSimple to write a temporary file
    f_good = open(output_prefix + '.gff', 'w')
    f_group = open('branch_tmp.group.txt', 'w')
    f_bad = f_good
    gene_index = 1
    for qid,indices in compressed_records_pointer_dict.iteritems():
        for isoform_index,i in enumerate(indices):
            bs.cuff_index = gene_index # for set to the same
            records = merged_exons[i]
            bs.process_records(records, allow_extra_5_exons, skip_5_exon_alt, \
                    f_good, f_bad, f_group, tolerate_end=100, \
                    starting_isoform_index=isoform_index, gene_prefix='PBfusion')
        gene_index += 1
    f_good.close()
    f_bad.close()
    f_group.close()

    # step (4). read the tmp file and modify to display per fusion gene
    f_group = open(output_prefix + '.group.txt', 'w')
    count = 0
    with open('branch_tmp.group.txt') as f:
        while True:
            line = f.readline().strip()
            if len(line) == 0: break
            pbid1, groups1 = line.strip().split('\t')
            pbid2, groups2 = f.readline().strip().split('\t')
            assert pbid1.split('.')[1] == pbid2.split('.')[1]
            group = set(groups1.split(',')).intersection(groups2.split(','))
            f_group.write("{0}\t{1}\n".format(pbid1[:pbid1.rfind('.')], ",".join(group)))
            count += 1
    f_group.close()
    os.remove('branch_tmp.group.txt')

    print >> sys.stderr, "{0} fusion candidates identified.".format(count)
    print >> sys.stderr, "Output written to: {0}.gff, {0}.group.txt".format(output_prefix)

    # (optional) step 5. get count information
    if prefix_dict_pickle_filename is not None:
        with open(prefix_dict_pickle_filename) as f:
            d = load(f)
            d1 = d['HQ']
            d1.update(d['LQ'])
        tofu_wrap.get_abundance(output_prefix, d1, output_prefix)
        print >> sys.stderr, "Count information written to: {0}.abundance.txt".format(output_prefix)

if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument("--input", help="Input FA/FQ filename")
    parser.add_argument("--fq", default=False, action="store_true", help="Input is a fastq file (default is fasta)")
    parser.add_argument("-s", "--sam", required=True, help="Sorted GMAP SAM filename")
    parser.add_argument("-o", "--prefix", required=True, help="Output filename prefix")
    parser.add_argument("--dun-merge-5-shorter", action="store_false", dest="allow_extra_5exon", default=True, help="Don't collapse shorter 5' transcripts (default: turned off)")
    parser.add_argument("--prefix_dict_pickle_filename", default=None, help="Quiver HQ/LQ Pickle filename for generating count information (optional)")
    
    args = parser.parse_args()

    fusion_main(args.input, args.sam, args.prefix, is_fq=args.fq, allow_extra_5_exons=args.allow_extra_5exon, skip_5_exon_alt=False, prefix_dict_pickle_filename=args.prefix_dict_pickle_filename)


