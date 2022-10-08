from oai_agents.common.state_encodings import ENCODING_SCHEMES
from oai_agents.common.subtasks import Subtasks, calculate_completed_subtask

from overcooked_ai_py.mdp.overcooked_mdp import OvercookedGridworld, Action, Direction
from overcooked_ai_py.mdp.overcooked_env import OvercookedEnv
from overcooked_ai_py.planning.planners import MediumLevelActionManager
from overcooked_ai_py.visualization.state_visualizer import StateVisualizer

from copy import deepcopy
from gym import Env, spaces, register
import numpy as np
import pygame
from pygame.locals import HWSURFACE, DOUBLEBUF, RESIZABLE
from stable_baselines3.common.env_checker import check_env


USEABLE_COUNTERS = 5


class OvercookedGymEnv(Env):
    metadata = {'render.modes': ['human']}

    def __init__(self, call_init=True, **kwargs):
        if call_init:
            self.init(**kwargs)

    def init(self, index=None, play_both_players=False, base_env=None, horizon=None, grid_shape=None, is_eval_env=False,
                   shape_rewards=False, ret_completed_subtasks=False, args=None):
        '''
        :param play_both_players: If true, play actions of both players. Step requires tuple(int, int) instead of int
        :param grid_shape: Shape over
        :param shape_rewards: Shape rewards for RL
        :param base_env: Base overcooked environment. If None, create env from layout name. Useful if special parameters
                         are required when creating the environment
        :param horizon: How many steps to run the env for. If None, default to args.horizon value
        :param args: Experiment arguments (see arguments.py)
        '''
        assert index is not None
        self.layout_name = args.layout_names[index]
        self.play_both_players = play_both_players
        if base_env is None:
            self.mdp = OvercookedGridworld.from_layout_name(self.layout_name)
            horizon = horizon or args.horizon
            all_counters = self.mdp.get_counter_locations()
            COUNTERS_PARAMS = {
                'start_orientations': False,
                'wait_allowed': False,
                'counter_goals': all_counters,
                'counter_drop': all_counters,
                'counter_pickup': all_counters,
                'same_motion_goals': True
            }
            self.mlam = MediumLevelActionManager.from_pickle_or_compute(self.mdp, COUNTERS_PARAMS, force_compute=False)
            # To ensure that an agent is on both sides of the counter, remove random starts for forced coord
            ss_fn = self.mdp.get_fully_random_start_state_fn(self.mlam)
            self.env = OvercookedEnv.from_mdp(self.mdp, horizon=horizon, start_state_fn=ss_fn)
        else:
            self.env = base_env
        self.grid_shape = grid_shape or self.env.mdp.shape
        self.is_eval_env = is_eval_env
        self.shape_rewards = shape_rewards
        self.return_completed_subtasks = ret_completed_subtasks
        self.args = args
        self.device = args.device
        self.encoding_fn = ENCODING_SCHEMES[args.encoding_fn]
        self.visualization_enabled = False
        self.step_count = 0
        self.teammate = None
        self.terrain = self.mdp.terrain_mtx
        self.prev_st = Subtasks.SUBTASKS_TO_IDS['unknown']
        obs = self.reset()
        self.visual_obs_shape = obs['visual_obs'].shape if 'visual_obs' in obs else 0
        self.agent_obs_shape = obs['agent_obs'].shape if 'agent_obs' in obs else 0
        # TODO improve bounds for each dimension
        # Currently 20 is the default value for recipe time (which I believe is the largest value used
        self.obs_dict = {}
        if np.prod(self.visual_obs_shape) > 0:
            self.obs_dict['visual_obs'] = spaces.Box(0, 20, self.visual_obs_shape, dtype=np.int)
        if np.prod(self.agent_obs_shape) > 0:
            self.obs_dict['agent_obs'] = spaces.Box(0, self.args.horizon, self.agent_obs_shape, dtype=np.float32)
        if ret_completed_subtasks:
            self.obs_dict['player_completed_subtasks'] = spaces.Discrete(Subtasks.NUM_SUBTASKS + 1)
            self.obs_dict['teammate_completed_subtasks'] = spaces.Discrete(Subtasks.NUM_SUBTASKS + 1)
            self.obs_dict['subtask_mask'] = spaces.MultiBinary(Subtasks.NUM_SUBTASKS)
        self.observation_space = spaces.Dict(self.obs_dict)

        if play_both_players:  # We control both agents
            self.action_space = spaces.MultiDiscrete([len(Action.ALL_ACTIONS), len(Action.ALL_ACTIONS)])
        else:  # We control 1 agent
            self.action_space = spaces.Discrete(len(Action.ALL_ACTIONS))
            self.teammate = None

    def set_teammate(self, teammate):
        self.teammate = teammate

    def setup_visualization(self):
        self.visualization_enabled = True
        pygame.init()
        surface = StateVisualizer().render_state(self.state, grid=self.env.mdp.terrain_mtx)
        self.window = pygame.display.set_mode(surface.get_size(), HWSURFACE | DOUBLEBUF | RESIZABLE)
        self.window.blit(surface, (0, 0))
        pygame.display.flip()

    def action_masks(self):
        return get_doable_subtasks(self.state, self.prev_st, self.terrain, self.p_idx, USEABLE_COUNTERS).astype(bool)

    def get_obs(self, p_idx=None):
        obs = self.encoding_fn(self.env.mdp, self.state, self.grid_shape, self.args.horizon, p_idx=p_idx)
        if self.return_completed_subtasks:
            obs['subtask_mask'] = self.action_masks()
            if self.prev_state is None:
                obs['player_completed_subtasks'] = Subtasks.SUBTASKS_TO_IDS['unknown']
                obs['teammate_completed_subtasks'] = Subtasks.SUBTASKS_TO_IDS['unknown']
            else:
                comp_st = [calculate_completed_subtask(self.terrain, self.prev_state, self.state, i) for i in range(2)]
                # If no subtask is completed, set it to one number greater than a possible subtask number
                p_comp_st = comp_st[p_idx] if comp_st[p_idx] is not None else Subtasks.NUM_SUBTASKS
                t_comp_st = comp_st[1 - p_idx] if comp_st[1 - p_idx] is not None else Subtasks.NUM_SUBTASKS
                obs['player_completed_subtasks'] = p_comp_st
                obs['teammate_completed_subtasks'] = t_comp_st
                if p_comp_st is not None:
                    self.prev_st = p_comp_st
        return obs

    def step(self, action):
        if not self.play_both_players and self.teammate is None:
            raise ValueError('set_teammate must be set called before starting game unless play_both_players is True')
        if self.play_both_players: # We control both agents
            joint_action = action
        else: # We control 1 agent
            joint_action = [None, None]
            joint_action[self.p_idx] = action
            joint_action[self.t_idx] = self.teammate.predict(self.get_obs(p_idx=self.t_idx))[0]

        joint_action = [Action.INDEX_TO_ACTION[a] for a in joint_action]

        # If the state didn't change from the previous timestep and the agent is choosing the same action
        # then play a random action instead. Prevents agents from getting stuck
        if self.prev_state and self.state.time_independent_equal(self.prev_state) and tuple(joint_action) == self.prev_actions:
            joint_action = [np.random.choice(Direction.ALL_DIRECTIONS), np.random.choice(Direction.ALL_DIRECTIONS)]

        self.prev_state, self.prev_actions = deepcopy(self.state), joint_action

        next_state, reward, done, info = self.env.step(joint_action)
        self.state = self.env.state
        if self.shape_rewards and not self.is_eval_env:
            ratio = min(self.step_count * self.args.n_envs / 2.5e6, 1)
            sparse_r = sum(info['sparse_r_by_agent'])
            shaped_r = info['shaped_r_by_agent'][self.p_idx] if self.p_idx else sum(info['shaped_r_by_agent'])
            reward = sparse_r * ratio + shaped_r * (1 - ratio)
        self.step_count += 1
        return self.get_obs(self.p_idx), reward, done, info

    def reset(self):
        if not self.play_both_players:
            self.p_idx = np.random.randint(2)
            self.t_idx = 1 - self.p_idx

        if self.is_eval_env:
            ss_kwargs = {'random_pos': False, 'random_dir': False, 'max_random_objs': 0}
        else:
            random_pos = (self.layout_name != 'forced_coordination')
            ss_kwargs = {'random_pos': random_pos, 'random_dir': True, 'max_random_objs': USEABLE_COUNTERS}
        self.env.reset(start_state_kwargs=ss_kwargs)
        self.prev_state = None
        self.state = self.env.state
        return self.get_obs(self.p_idx)

    def render(self, mode='human', close=False):
        if self.visualization_enabled:
            surface = StateVisualizer().render_state(self.state, grid=self.env.mdp.terrain_mtx)
            self.window = pygame.display.set_mode(surface.get_size(), HWSURFACE | DOUBLEBUF | RESIZABLE)
            self.window.blit(surface, (0, 0))
            pygame.display.flip()
            pygame.time.wait(200)

    def close(self):
        pygame.quit()


register(
    id='OvercookedGymEnv-v0',
    entry_point='OvercookedGymEnv'
)

class DummyAgent:
    def __init__(self, action=Action.STAY):
        self.action = Action.ACTION_TO_INDEX[action]

    def predict(self, x, state=None, episode_start=None, deterministic=False):
        return self.action, None

if __name__ == '__main__':
    from oai_agents.common.arguments import get_arguments
    args = get_arguments()
    env = OvercookedGymEnv(p1=DummyAgent(), args=args) #make('overcooked_ai.agents:OvercookedGymEnv-v0', layout='asymmetric_advantages', encoding_fn=encode_state, args=args)
    print(check_env(env))
    env.setup_visualization()
    env.reset()
    env.render()
    done = False
    while not done:
        obs, reward, done, info = env.step( Action.ACTION_TO_INDEX[np.random.choice(Action.ALL_ACTIONS)] )
        env.render()
