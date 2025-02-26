import htcondor
import htcondor.dags
import classad
import numpy as np
from itertools import product
from argparse import ArgumentParser

parser = ArgumentParser()
parser.add_argument("--shape-model", required=True, dest="shape_model")
parser.add_argument("--counts-model", required=True, dest="counts_model")
parser.add_argument("-o", required=True, dest="outfolder")
parser.add_argument("--dag-dir", required=True, dest="dag_dir")
parser.add_argument("--singularity-image", required=True, dest="simage")
parser.add_argument("--for-slurm", action="store_true", dest="for_slurm")
parser.add_argument("--repo-path", required=True, dest="repo_path")

args = parser.parse_args()

outfile_path = "fisher_$(spacing)_$(energy)_$(pmts)_$(seed)_tfirst.npz"

if not args.for_slurm:
    exec = f"{args.repo_path}/run.sh"
    exec_args = f"python {args.repo_path}/olympus/run_fisher.py -o {outfile_path} -s $(spacing) -e $(energy) --seed $(seed) --shape_model {args.shape_model} --counts_model {args.counts_model} --pmts $(pmts) --mode tfirst",
else:
    exec = f"singularity run --nv {args.singularity_image}"
    exec_args = f"PYTHONPATH={args.repo_path}/olympus:{args.repo_path}/hyperion python {args.repo_path}/olympus/run_fisher.py -o {outfile_path} -s $(spacing) -e $(energy) --seed $(seed) --shape_model {args.shape_model} --counts_model {args.counts_model} --pmts $(pmts) --mode tfirst",


description = htcondor.Submit(
    executable=exec,  # the program we want to run
    arguments=exec_args
    log="logs/log",  # the HTCondor job event log
    output="logs/fisher.out.$(spacing)_$(energy)_$(seed)_$(pmts)",  # stdout from the job goes here
    error="logs/fisher.err.$(spacing)_$(energy)_$(seed)_$(pmts)",  # stderr from the job goes here
    request_gpus="1",
    Requirements="HasSingularity",
    should_transfer_files="YES",
    when_to_transfer_output="ON_EXIT",
)
description["+SingularityImage"] = classad.quote(args.simage)

spacings = np.linspace(50, 200, 7)
energies = np.logspace(3, 5.5, 7)
seeds = np.arange(100)
pmts = [16, 20, 24]

dagvars = []
for spacing, energy, seed, pmt in product(spacings, energies, seeds, pmts):
    dagvars.append({"spacing": spacing, "energy": energy, "seed": seed, "pmts": pmt})

dag = htcondor.dags.DAG()

layer = dag.layer(
    name="fisher",
    submit_description=description,
    vars=dagvars,
)


dag_file = htcondor.dags.write_dag(dag, args.dag_dir, dag_file_name="fisher.dag")
