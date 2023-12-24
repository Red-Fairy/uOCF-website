#!/bin/bash
#SBATCH --account=viscam --partition=viscam,viscam-interactive,svl,svl-interactive --qos=normal
#SBATCH --nodes=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=32G

# only use the following on partition with GPUs
#SBATCH --gres=gpu:3090:1

#SBATCH --job-name="T_uORF"
#SBATCH --output=logs/%j.out

# only use the following if you want email notification
####SBATCH --mail-user=youremailaddress
####SBATCH --mail-type=ALL

# list out some useful information (optional)
echo "SLURM_JOBID="$SLURM_JOBID
echo "SLURM_JOB_NODELIST"=$SLURM_JOB_NODELIST
echo "SLURM_NNODES"=$SLURM_NNODES
echo "SLURMTMPDIR="$SLURMTMPDIR
echo "working directory = "$SLURM_SUBMIT_DIR

# sample process (list hostnames of the nodes you've requested)
DATAROOT=${1:-'/svl/u/redfairy/datasets/CLEVR/stack'}
PORT=${2:-12783}
python -m visdom.server -p $PORT &>/dev/null &
python train_without_gan.py --dataroot $DATAROOT --n_scenes 1000 --n_img_each_scene 2 \
    --checkpoints_dir 'checkpoints' --name 'CLEVR-stack' \
    --display_port $PORT --display_ncols 4 --print_freq 50 --display_freq 50 --save_epoch_freq 20 \
    --load_size 128 --n_samp 64 --input_size 128 --supervision_size 64 --frustum_size 64 \
    --model 'uocf_dual_DINO_trans' \
    --attn_decay_steps 100000 --bottom --warmup_steps 0 --obj_scale 5 \
    --encoder_size 896 --encoder_type 'DINO' \
    --num_slots 8 --attn_iter 6 --shape_dim 48 --color_dim 48 \
    --coarse_epoch 500 --niter 1000 --percept_in 100 --no_locality_epoch 300 --seed 2027 \
    --stratified --fixed_locality --fg_object_size 3 --dense_sample_epoch 300 --n_feat_layers 1 \
    --attn_dropout 0 --attn_momentum 0.5 --pos_init 'zero' \
    --fg_density_loss --bg_density_loss --collapse_prevent 10000 \
    --exp_id '1222-plane/4obj-scratch' \
    --dummy_info '' \

# can try the following to list out which GPU you have access to
#srun /usr/local/cuda/samples/1_Utilities/deviceQuery/deviceQuery

# done
echo "Done"
