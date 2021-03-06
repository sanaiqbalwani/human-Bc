import argparse
import gym
import numpy as np
import tensorflow as tf
import scipy.signal
import pandas as pd
# import matplotlib.pyplot as plt


from run_expert import get_expert_data
from rollouts import do_rollout
from envs import get_timesteps_per_episode



def get_minibatch(expert_data, mb_size=128):
    observations = expert_data["observations"]
    actions = expert_data["actions"]
    indices = np.arange(observations.shape[0])
    np.random.shuffle(indices)
    mb_observations = observations[indices[:mb_size], :]
    mb_actions = actions[indices[:mb_size], :].squeeze()
    return mb_observations, mb_actions


class PolicyPredictor():

    def __init__(self, args,session):
        self.args=args
        self.env = gym.make(args.envname)
        env=self.env
        self.sess = tf.InteractiveSession()
        self.sess=session
        self.obs_shape =env.observation_space.shape
        self.discrete_action_space = not hasattr(env.action_space, "shape")
        self.act_shape = (env.action_space.n,) if self.discrete_action_space else env.action_space.shape
        self.graph = self.build_graph()
        self.sess.run(tf.global_variables_initializer())
        self.sess.run(tf.local_variables_initializer())
        self.record=pd.DataFrame(columns=('iteration', 'loss_action', 'wighted_loss','rewards','name'))
       
        
        # self.saver.save(self.sess, 'policy.ckpt')

    
    def policy_rollout(self,reward_fn):
        paths = []
        env=self.env

        for itr in range(0,self.args.num_rollouts):
            

            # Run policy rollouts to get sample
            ob = env.reset()
            obs,acs, rewards = [], [], []
            horizon=int(self.args.num_timesteps)

            for hor in range(0,horizon):

                obs.append(ob)
                ac=self.get_pg_action(ob[None])
                try:
                    ac=np.squeeze(ac,axis=(0))
                except:
                    pass
                rew0=reward_fn.predict_reward(ob,ac)

                acs.append(ac)
                ob, rew, done, _ = env.step(ac)
                rewards.append(rew0)
                if done or  hor>horizon:
                    break

            path = {"observation" : np.array(obs),
                    "reward" : np.array(rewards),
                    "action" : np.array(acs)}
            paths.append(path)

        print('GENERATED POLICY ROLLOUT')
        return paths


    def build_mlp(self,inputplaceholder):
        with tf.variable_scope("policyPredictor"):
            self.layers = [tf.layers.dense(inputs= inputplaceholder, units=64, activation=tf.tanh, use_bias=True,
                                      kernel_initializer=tf.contrib.layers.xavier_initializer(uniform=True))]

            for ix in range(1, 3):
                new_layer = tf.layers.dense(inputs=self.layers[-1], units=64, activation=tf.tanh, use_bias=True,
                                            kernel_initializer=tf.contrib.layers.xavier_initializer(uniform=True))

                self.layers.append(new_layer)
            # import pdb;pdb.set_trace()

            out= tf.layers.dense(inputs=self.layers[-1], units=self.act_shape[-1], activation=None, use_bias=True,
                                                kernel_initializer=tf.contrib.layers.xavier_initializer(uniform=True))


            return out

    def build_graph(self):
        env=self.env
        """Creates graph of the neural net"""
        self.x= tf.placeholder(tf.float32, shape=[None, self.obs_shape[-1]])
        self.y= tf.placeholder(tf.float32, shape=[None, self.act_shape[-1]])
        self.reward = tf.placeholder(tf.float32, [None,1])

            # import pdb;pdb.set_trace()

        self.nn_policy_a = self.build_mlp(self.x)
        # bc-clone
        # Construct the loss function and training information.
        self.loss_op = tf.reduce_mean(tf.reduce_sum((self.nn_policy_a - self.y) * (self.nn_policy_a - self.y), axis=[1]))
        # global_step = tf.Variable(0, name='global_step', trainable=False)
        self.train_step = tf.train.AdamOptimizer(self.args.bc_learning_rate).minimize(self.loss_op,global_step=tf.contrib.framework.get_global_step())
        

        # _____policy gradient==discrete___:

        # action_mask = tf.one_hot(self.nn_policy_a, int(self.act_shape[-1]), 1,0)
        # self.action_prob = tf.nn.softmax(self.nn_policy_a)
        # self.action_value_pred = tf.reduce_sum(self.action_prob * action_mask, 1)

        # self.loss_pg = tf.reduce_mean(-tf.log(self.action_value_pred) * self.target)
        # # self.optimizer = tf.train.AdamOptimizer(learning_rate=.001)
        # # # self.train_op_pg = self.optimizer.minimize(self.pg_loss, global_step=tf.contrib.framework.get_global_step())

        # self.train_op_pg = tf.train.AdamOptimizer(self.args.bc_learning_rate).minimize(self.loss_pg)


        # _____policy gradient==continuous___:
        # we  assume normal distribution on actions find 
        # sigma and mean of distribution

        self.mu = tf.squeeze(self.nn_policy_a)
        self.sigma = tf.exp(tf.get_variable(name='variance',shape=[self.act_shape[-1]], initializer=tf.zeros_initializer()))
        # logstd should just be a trainable variable, not a network output.
        self.sigma = tf.nn.softplus(self.sigma) + 1e-5
        
        self.normal_dist = tf.contrib.distributions.Normal(self.mu, self.sigma)
        self.action = self.normal_dist._sample_n(1)
    
        self.action = tf.clip_by_value(self.action, env.action_space.low[0], env.action_space.high[0])
      
        # Loss and train op

        self.loss_pg = -tf.reduce_mean(self.normal_dist.log_prob(self.action) * self.reward)
        self.train_op_pg = tf.train.AdamOptimizer(self.args.bc_learning_rate).minimize(self.loss_pg)

        return tf.get_default_graph()


    def train_bc_policy(self):
        print("="*30)
        env = self.env
        expert_data, _ = get_expert_data(self.args.expert_policy_file, self.args.envname, self.args.max_timesteps, self.args.num_rollouts)
        print("Got rollouts from expert.")

        for i in range(self.args.bc_training_epochs+1):
            with self.graph.as_default():
                mb_obs, mb_acts = get_minibatch(expert_data, self.args.bc_minibatch_size)
                _, training_loss = self.sess.run([self.train_step, self.loss_op], feed_dict={self.x: mb_obs, self.y: mb_acts})
                reward_actual_in_env=self.get_rewards()

                self.record = self.record.append([{'iteration':i,'loss_action':training_loss,'wighted_loss':0,'rewards':reward_actual_in_env,'name':'bc'}], ignore_index=True)

            if i%50==0:
                print('BC LOSS:',i,training_loss)
                print('BC mean REWARDS:',i,'--',reward_actual_in_env)
        print("Expert policy cloned.")
        print("="*30)
        # r=self.get_rewards(render=True)



        return self.nn_policy_a,self.x

    def train_policy_grad(self,reward_fn):
        env = gym.make(self.args.envname)
        paths=self.policy_rollout(reward_fn)
        mb_obs = np.concatenate([path["observation"] for path in paths])
        mb_acts = np.concatenate([path["action"] for path in paths])
        reward_obtained = np.concatenate([self.discount(path["reward"]) for path in paths])

        

        print()
        print(mb_obs.shape,mb_acts.shape,reward_obtained.shape)
        # reward_obtained = np.concatenate([path["reward"].sum() for path in paths])


        total_timesteps = 0
        epoch_loss_reward_all=[]
        for i in range(self.args.policy_iter+1):
            timesteps_this_batch = 0
            epoch_loss_reward=[]
            cost=0
            for path in range(len(paths)):
                with self.graph.as_default():
                    _, training_loss,bc_loss = self.sess.run([self.train_op_pg , self.loss_pg,self.loss_op], feed_dict={self.x: mb_obs, self.y: mb_acts,self.reward:reward_obtained})
                cost=np.mean(training_loss)
                bc_cost=bc_loss
                reward_actual_in_env=self.get_rewards()
                self.record = self.record.append([{'iteration':i,'loss_action':bc_loss,'wighted_loss':training_loss,'rewards':reward_actual_in_env,'name':'pg'+str(self.args.num_iters)}], ignore_index=True)

            if i%20==0:

                print('policy gradient Mean Loss:',i,cost)
                print('policy gradient Mean Loss  diff actions:',i,bc_cost)
                print('policy gradient Mean Reward:',i,'--',reward_actual_in_env)
        print("finished policy gradient.")
        print("="*30)
        # r=self.get_rewards(render=True)
        return self.nn_policy_a,self.x
        



    def predict_action_bc(self, state):
        """Predict the action for each state"""
        with self.graph.as_default():
            ac = self.sess.run(self.nn_policy_a, feed_dict={self.x:state})
        return ac

    def get_pg_action(self, state):
        with self.graph.as_default():
            action = self.sess.run(self.action,feed_dict={self.x:state})
        return action


    def get_rewards(self,render=False):
        returns = []
        horizon=int(self.args.num_timesteps)

        for _ in range(self.args.num_rollouts+1):
            obs = self.env.reset()
            done = False
            totalr = 0
            steps = 0
            while not done:
                # Take steps by expanding observation (to get shapes to match).
                exp_obs = np.expand_dims(obs, axis=0)
                action = np.squeeze(self.sess.run(self.nn_policy_a, feed_dict={self.x: exp_obs}))
                obs, r, done, _ = self.env.step(action)
                totalr += r
                steps += 1
                if render: self.env.render()
                if steps >= horizon: break
            returns.append(totalr)

        return np.mean(returns)

    def discount(self,x, gamma=1):
        """
        Compute discounted sum of future values. Returns a list, NOT a scalar!
        out[i] = in[i] + gamma * in[i+1] + gamma^2 * in[i+2] + ...
        """
        return scipy.signal.lfilter([1],[1,-gamma],x[::-1], axis=0)[::-1]

    def load_policy(self):
        self.saver.restore(self.sess, 'policy.ckpt')
                 


