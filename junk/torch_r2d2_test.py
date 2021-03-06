# A simplified implementation of the R2D2 architecture, using the memory game as a benchmark to test recurrent performance
import gym
from gym.spaces import Discrete, Box
import numpy as np
import time
import torch
import torch.nn as nn
from torch.optim import Adam
from typing import Optional, Tuple


class Stopwatch:

    def __init__(self):
        self._started = None
        self._elapsed = 0

    def start(self):
        if self._started is None:
            self._started = time.time()

    def restart(self):
        self._elapsed = 0
        self._started = time.time()

    def stop(self):
        stopped = time.time()
        if self._started is not None:
            self._elapsed += stopped - self._started
            self._started = None

    def reset(self):
        self._elapsed = 0
        self._started = None

    def elapsed(self):
        return self._elapsed


class LSTMNet(nn.Module):
    
    def __init__(self, observation_space, action_space, hidden_size=32, hidden_layers=1, deuling=False):
        super(LSTMNet, self).__init__()
        self._hidden_size = hidden_size
        self._hidden_layers = hidden_layers
        self._deuling = deuling

        input_size = int(np.prod(observation_space.shape))  # NOTE: Need to cast for TorchScript to work
        self._lstm = nn.LSTM(input_size, hidden_size, hidden_layers)
        self._q_function = nn.Linear(hidden_size, action_space.n)

        if deuling:
            self._value_function = nn.Linear(hidden_size, 1)

    def forward(self, 
            obs: torch.Tensor, 
            hidden: Optional[Tuple[torch.Tensor, torch.Tensor]]=None):
        outputs, hidden = self._lstm(obs, hidden)
        Q = self._q_function(outputs)

        if self._deuling:
            V = self._value_function(outputs)
            Q += V - Q.mean(2, keepdim=True)

        return Q, hidden

    @torch.jit.export
    def get_h0(self, batch_size: int=1):
        shape = [self._hidden_layers, batch_size, self._hidden_size]  # NOTE: Shape must be a list for TorchScript serialization to work

        hidden = torch.zeros(shape, dtype=torch.float32)
        cell = torch.zeros(shape, dtype=torch.float32)
        
        return hidden, cell


class ReplayBuffer:
    
    def __init__(self, action_space, capacity=128, device='cpu'):
        self._action_space = action_space
        self._capacity = capacity
        self._device = device

        self._index = 0
        self._obs = []
        self._actions = []
        self._rewards = []
        self._dones = []
    
    def add(self, obs, actions, rewards, dones):
        obs = torch.tensor(obs, dtype=torch.float32, device=self._device)
        actions = torch.tensor(actions, dtype=torch.int64, device=self._device)
        actions = nn.functional.one_hot(actions, self._action_space.n)
        rewards = torch.tensor(rewards, dtype=torch.float32, device=self._device)
        dones = torch.tensor(dones, dtype=torch.float32, device=self._device)

        if len(obs) < self._capacity:
            self._obs.append(obs)
            self._actions.append(actions)
            self._rewards.append(rewards)
            self._dones.append(dones)
        else:
            self._obs[self._index] = obs
            self._actions[self._index] = actions
            self._rewards[self._index] = rewards
            self._dones[self._index] = dones

        self._index = (self._index + 1) % self._capacity
    
    def sample(self, batch_size):
        indices = np.random.randint(len(self._actions), size=batch_size)
        
        obs_batch = [self._obs[idx][:-1] for idx in indices]
        next_obs_batch = [self._obs[idx][1:] for idx in indices]
        
        action_batch = [self._actions[idx] for idx in indices]
        reward_batch = [self._rewards[idx] for idx in indices]
        done_batch = [self._dones[idx] for idx in indices]
        mask = [torch.ones_like(self._dones[idx]) for idx in indices]

        obs_batch = nn.utils.rnn.pad_sequence(obs_batch)
        next_obs_batch = nn.utils.rnn.pad_sequence(next_obs_batch)
        action_batch = nn.utils.rnn.pad_sequence(action_batch)
        reward_batch = nn.utils.rnn.pad_sequence(reward_batch)
        done_batch = nn.utils.rnn.pad_sequence(done_batch)
        mask = nn.utils.rnn.pad_sequence(mask)

        return obs_batch, next_obs_batch, action_batch, reward_batch, done_batch, mask


class Policy:

    def __init__(self, model):
        self._model = model
        self._state = None

    def reset(self, batch_size=1):
        self._state = self._model.get_h0(batch_size)

    def act(self, obs, explore=False):
        q_values, self._state = self._model(obs.reshape([1,1,-1]), self._state)

        return q_values.reshape([-1]).argmax()


class R2D2:

    def __init__(self, 
                env, 
                num_episodes=8, 
                buffer_size=2048,
                batch_size=4,
                num_batches=4,
                sync_interval=4,
                epsilon=0.05,
                gamma=0.99,
                beta=0.5,
                lr=0.01,
                hidden_size=64,
                hidden_layers=1,
                deuling=True,
                device='cpu'):
        self._env = env
        self._num_episodes = num_episodes
        self._batch_size = batch_size
        self._num_batches = num_batches
        self._sync_interval = sync_interval
        self._epsilon = epsilon
        self._gamma = gamma
        self._beta = beta
        self._device = device

        self._replay_buffer = ReplayBuffer(env.action_space, buffer_size, device)

        self._online_network = LSTMNet(env.observation_space, env.action_space, hidden_size, hidden_layers, deuling)
        self._target_network = LSTMNet(env.observation_space, env.action_space, hidden_size, hidden_layers, deuling)
        
        self._online_network = torch.jit.script(self._online_network)
        self._target_network = torch.jit.script(self._target_network)

        # Optional: move models to GPU
        self._online_network.to(device)
        self._target_network.to(device)
        
        self._optimizer = Adam(self._online_network.parameters(), lr=lr)
        self._iterations = 0

        self._state = None

    def _loss(self, obs_batch, next_obs_batch, action_batch, reward_batch, done_batch, mask):
        h0 = self._online_network.get_h0(obs_batch.shape[1])
        h0 = (h0[0].to(self._device), h0[1].to(self._device))
        online_q, _ = self._online_network(obs_batch, h0)  # Need batched history
        target_q, _ = self._target_network(next_obs_batch, h0)

        q_targets = reward_batch + self._gamma * (1 - done_batch) * target_q.max(-1).values
        online_q = (action_batch * online_q).sum(-1)

        errors = nn.functional.smooth_l1_loss(online_q, q_targets.detach(), beta=self._beta, reduction='none')
        return torch.mean(mask * errors)

    def reset(self, batch_size=1):
        self._state = self._online_network.get_h0(batch_size)
        self._state = (self._state[0].to(self._device), self._state[1].to(self._device))

    def act(self, obs, explore=True):
        q_values, self._state = self._online_network(obs.reshape([1,1,-1]), self._state)

        if explore and np.random.random() <= self._epsilon:
            return self._env.action_space.sample()

        return q_values.reshape([-1]).argmax()

    def train(self):
        self._iterations += 1
        if self._iterations % self._sync_interval == 0:
            parameters = self._online_network.state_dict()
            self._target_network.load_state_dict(parameters)

        for _ in range(self._num_episodes):
            observations = []
            actions = []
            rewards = []
            dones = []

            self.reset()
            obs = self._env.reset()
            observations.append(obs)
            done = False

            while not done:
                action = self.act(torch.as_tensor(obs, dtype=torch.float32, device=self._device))
                obs, reward, done, _ = self._env.step(action)

                observations.append(obs)
                actions.append(action)
                rewards.append(reward)
                dones.append(done)

            self._replay_buffer.add(observations, actions, rewards, dones)

        for _ in range(self._num_batches):
            batch = self._replay_buffer.sample(self._batch_size)
            self._optimizer.zero_grad()
            loss = self._loss(*batch).mean()
            loss.backward()
            self._optimizer.step()
    
    def save(self, path):
        torch.jit.save(self._online_network, path)


def evaluate(env, policy, num_episodes=30, device='cpu'):
    total_reward = 0
    total_successes = 0

    for _ in range(num_episodes):
        policy.reset()
        obs = env.reset()
        episode_reward = 0
        done = False

        while not done:
            action = policy.act(torch.as_tensor(obs, dtype=torch.float32, device=device), explore=False)
            obs, reward, done, _ = env.step(action)
            episode_reward += reward
        
        total_reward += episode_reward
        if episode_reward > 0:
            total_successes += 1

    return (total_reward / num_episodes), (total_successes / num_episodes)


class MemoryGame(gym.Env):
    '''An instance of the memory game with noisy observations'''

    def __init__(self, length=5, num_cues=2, noise=0.1):
        self.observation_space = Box(0, 2, shape=(num_cues + 2,))
        self.action_space = Discrete(num_cues)
        self._length = length
        self._num_cues = num_cues 
        self._noise = noise       
        self._current_step = 0
        self._current_cue = 0

    def _obs(self):
        obs = np.random.uniform(0, self._noise, self.observation_space.shape)
        if 0 == self._current_step:
            obs[-2] += 1
            obs[self._current_cue] += 1
        elif self._length == self._current_step:
            obs[-1] += 1
        return obs

    def reset(self):
        self._current_step = 0
        self._current_cue = np.random.randint(self._num_cues)
        return self._obs()

    def step(self, action):
        if self._current_step < self._length:
            self._current_step += 1
            return self._obs(), 0, False, {}
        else:
            reward = (1 if action == self._current_cue else 0)
            return self._obs(), reward, True, {}


class CoordinationGame(gym.Env):
    
    class Fixed:
        
        def __init__(self, actions):
            self._actions = actions
            self._current_action = None

        def reset(self):
            self._current_action = np.random.randint(self._actions)

        def act(self, opponent_action=None):
            return self._current_action

    class SelfPlay:
        
        def __init__(self, actions):
            self._actions = actions

        def reset(self):
            pass

        def act(self, opponent_action=None):
            if opponent_action is None:
                return np.random.randint(self._actions)

            return opponent_action

    class FictitiousPlay:
        def __init__(self, actions):
            self._actions = actions
            self._counts = np.zeros(actions, dtype=np.int32)

        def reset(self):
            self._counts.fill(0)

        def act(self, opponent_action=None):
            if opponent_action is None:
                return np.random.randint(self._actions)

            self._counts[opponent_action] += 1
            return np.argmax(self._counts)

    def __init__(self, rounds=5, actions=2, partners=['fixed']):
        self.observation_space = Box(0, 1, shape=(actions,))
        self.action_space = Discrete(actions)
        self._rounds = rounds

        self._partners = []
        for partner in set(partners):
            if 'fixed' == partner:
                self._partners.append(self.Fixed(actions))
            elif 'sp' == partner:
                self._partners.append(self.SelfPlay(actions))
            elif 'fp' == partner:
                self._partners.append(self.FictitiousPlay(actions))
            else:
                raise ValueError(f"No partner type '{partner}'")
        
        self._current_round = 0
        self._current_partner = None
        self._opponent_action = None

    def reset(self):
        self._current_round = 0
        self._current_partner = np.random.choice(self._partners)
        self._current_partner.reset()
        self._opponent_action = None
        return np.zeros(self.action_space.n)

    def step(self, action):
        partner_action = self._current_partner.act(self._opponent_action)
        self._opponent_action = action

        obs = np.zeros(self.action_space.n)
        obs[partner_action] = 1

        reward = 1 if action == partner_action else 0
        
        self._current_round += 1
        done = self._rounds <= self._current_round
        
        return obs, reward, done, {}


if __name__ == "__main__":
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    stopwatch = Stopwatch()
    stopwatch.start()

    training_epochs = 500

    env = MemoryGame(10, 4)
    # env = CoordinationGame(20, 16, ['fixed'])
    agent = R2D2(env, device=device)

    print("\n===== Training =====")

    for epoch in range(training_epochs):
        agent.train()
        mean_reward, success_rate = evaluate(env, agent, device=device)

        print(f"\n----- Epoch {epoch + 1} -----")
        print(f"    mean return: {mean_reward}")
        print(f"    success rate: {success_rate * 100}%")
    
    # agent.save("torch_r2d2.pt")

    # model = torch.jit.load("torch_r2d2.pt")
    # policy = Policy(model)

    # mean_reward, success_rate = evaluate(env, policy)

    # print(f"\n----- Serialized Model -----")
    # print(f"    mean return: {mean_reward}")
    # print(f"    success rate: {success_rate * 100}%")

    stopwatch.stop()
    print(f"\nElapsed Time: {stopwatch.elapsed()}s")
