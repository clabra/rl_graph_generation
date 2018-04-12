#!/usr/bin/env python3

from mpi4py import MPI
from baselines.common import set_global_seeds
from baselines import bench
import os.path as osp
from baselines import logger
from baselines.common.atari_wrappers import make_atari, wrap_deepmind
from baselines.common.cmd_util import atari_arg_parser
from tensorboardX import SummaryWriter


import gym
import gym_molecule

def train(env_id, num_timesteps, seed,writer=None):
    from baselines.ppo1 import pposgd_simple_gcn, gcn_policy
    import baselines.common.tf_util as U
    rank = MPI.COMM_WORLD.Get_rank()
    sess = U.single_threaded_session()
    sess.__enter__()
    if rank == 0:
        logger.configure()
    else:
        logger.configure(format_strs=[])
    workerseed = seed + 10000 * MPI.COMM_WORLD.Get_rank()
    set_global_seeds(workerseed)
    env = gym.make('molecule-v0')
    print(env.observation_space)
    def policy_fn(name, ob_space, ac_space): #pylint: disable=W0613
        # return cnn_policy.CnnPolicy(name=name, ob_space=ob_space, ac_space=ac_space)
        return gcn_policy.GCNPolicy(name=name, ob_space=ob_space, ac_space=ac_space, atom_type_num=env.atom_type_num)
    # env = bench.Monitor(env, logger.get_dir() and
    #     osp.join(logger.get_dir(), str(rank)))
    env.seed(workerseed)

    # env = wrap_deepmind(env)
    # env.seed(workerseed)

    pposgd_simple_gcn.learn(env, policy_fn,
        max_timesteps=int(num_timesteps * 1.1),
        timesteps_per_actorbatch=64,
        clip_param=0.2, entcoeff=0.01,
        optim_epochs=4, optim_stepsize=1e-3, optim_batchsize=32,
        gamma=0.99, lam=0.95,
        schedule='linear', writer=writer
    )
    env.close()

def main():
    args = atari_arg_parser().parse_args()
    # writer = SummaryWriter()
    try:
        train(args.env, num_timesteps=args.num_timesteps, seed=args.seed,writer=None)
    except:
        # writer.export_scalars_to_json("./all_scalars.json")
        # writer.close()
        pass

if __name__ == '__main__':
    main()