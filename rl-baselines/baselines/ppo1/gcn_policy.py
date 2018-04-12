import tensorflow as tf
import numpy as np
import gym
import gym_molecule
from baselines.common.distributions import make_pdtype,MultiCatCategoricalPdType,CategoricalPdType
import baselines.common.tf_util as U


# gcn mean aggregation over edge features
def GCN(adj, node_feature, out_channels, is_act=True, is_normalize=False, name='gcn_simple'):
    '''
    state s: (adj,node_feature)
    :param adj: b*n*n
    :param node_feature: 1*n*d
    :param out_channels: scalar
    :param name:
    :return:
    '''
    edge_dim = adj.get_shape()[0]
    in_channels = node_feature.get_shape()[-1]
    with tf.variable_scope(name,reuse=tf.AUTO_REUSE):
        W = tf.get_variable("W", [edge_dim, in_channels, out_channels])
        b = tf.get_variable("b", [edge_dim, 1, out_channels])
        node_embedding = adj@tf.tile(node_feature,[edge_dim,1,1])@W+b
        if is_act:
            node_embedding = tf.nn.relu(node_embedding)
        # todo: try complex aggregation
        node_embedding = tf.reduce_mean(node_embedding,axis=0,keepdims=True) # mean pooling
        if is_normalize:
            node_embedding = tf.nn.l2_normalize(node_embedding,axis=-1)
        return node_embedding

# gcn mean aggregation over edge features
def GCN_batch(adj, node_feature, out_channels, is_act=True, is_normalize=False, name='gcn_simple'):
    '''
    state s: (adj,node_feature)
    :param adj: none*b*n*n
    :param node_feature: none*1*n*d
    :param out_channels: scalar
    :param name:
    :return:
    '''
    edge_dim = adj.get_shape()[1]
    batch_size = tf.shape(adj)[0]
    in_channels = node_feature.get_shape()[-1]

    with tf.variable_scope(name,reuse=tf.AUTO_REUSE):
        W = tf.get_variable("W", [1, edge_dim, in_channels, out_channels],initializer=tf.glorot_uniform_initializer())
        b = tf.get_variable("b", [1, edge_dim, 1, out_channels])
        node_embedding = adj@tf.tile(node_feature,[1,edge_dim,1,1])@tf.tile(W,[batch_size,1,1,1])+b # todo: tf.tile sum the gradients, may need to change
        if is_act:
            node_embedding = tf.nn.relu(node_embedding)
        # todo: try complex aggregation
        node_embedding = tf.reduce_mean(node_embedding,axis=1,keepdims=True) # mean pooling
        if is_normalize:
            node_embedding = tf.nn.l2_normalize(node_embedding,axis=-1)
        return node_embedding

def bilinear(emb_1, emb_2, name='bilinear'):
    node_dim = emb_1.get_shape()[-1]
    batch_size = tf.shape(emb_1)[0]
    with tf.variable_scope(name,reuse=tf.AUTO_REUSE):
        W = tf.get_variable("W", [1, node_dim, node_dim])
        return emb_1 @ tf.tile(W,[batch_size,1,1]) @ tf.transpose(emb_2,[0,2,1])

def bilinear_multi(emb_1, emb_2, out_dim, name='bilinear'):
    node_dim = emb_1.get_shape()[-1]
    batch_size = tf.shape(emb_1)[0]
    with tf.variable_scope(name,reuse=tf.AUTO_REUSE):
        W = tf.get_variable("W", [1,out_dim, node_dim, node_dim])
        emb_1 = tf.tile(tf.expand_dims(emb_1,axis=1),[1,out_dim,1,1])
        emb_2 = tf.transpose(emb_2,[0,2,1])
        emb_2 = tf.tile(tf.expand_dims(emb_2,axis=1),[1,out_dim,1,1])
        return emb_1 @ tf.tile(W,[batch_size,1,1,1]) @ emb_2



class GCNPolicy(object):
    recurrent = False
    def __init__(self, name, ob_space, ac_space, kind='small', atom_type_num = None):
        with tf.variable_scope(name):
            self._init(ob_space, ac_space, kind, atom_type_num)
            self.scope = tf.get_variable_scope().name

    def _init(self, ob_space, ac_space, kind, atom_type_num):
        self.pdtype = MultiCatCategoricalPdType
        ### 0 Get input
        ob = {'adj': U.get_placeholder(name="adj", dtype=tf.float32, shape=[None,ob_space['adj'].shape[0],None,None]),
              'node': U.get_placeholder(name="node", dtype=tf.float32, shape=[None,1,None,ob_space['node'].shape[2]])}
        # only when evaluating given action, at training time
        self.ac_real = U.get_placeholder(name='ac_real', dtype=tf.int64, shape=[None,3]) # feed groudtruth action
        if kind == 'small':
            self.emb_node1 = GCN_batch(ob['adj'], ob['node'], 32, name='gcn1')
            self.emb_node2 = GCN_batch(ob['adj'], self.emb_node1, 32, is_act=False, is_normalize=True, name='gcn2')
            emb_node = tf.squeeze(self.emb_node2,axis=1)  # B*n*f
        else:
            raise NotImplementedError

        ### 1 only keep effective nodes
        # ob_mask = tf.cast(tf.transpose(tf.reduce_sum(ob['node'],axis=-1),[0,2,1]),dtype=tf.bool) # B*n*1
        ob_len = tf.reduce_sum(tf.squeeze(tf.reduce_sum(ob['node'], axis=-1),axis=-2),axis=-1)  # B
        ob_len_first = ob_len-atom_type_num # todo: add a parameter for 3, number of node types
        logits_mask = tf.sequence_mask(ob_len, maxlen=tf.shape(ob['node'])[2]) # mask all valid entry
        logits_first_mask = tf.sequence_mask(ob_len_first,maxlen=tf.shape(ob['node'])[2]) # mask valid entry -3 (rm isolated nodes)

        ### 2 get graph embedding
        emb_graph = tf.reduce_max(emb_node, axis=1)  # max pooling

        ### 3.1: select first (active) node
        # rules: only select effective nodes
        self.logits_first = tf.layers.dense(emb_node, 32, activation=tf.nn.relu, name='linear_select1') # todo: do not select isolated nodes!!
        self.logits_first = tf.squeeze(tf.layers.dense(self.logits_first, 1, activation=None, name='linear_select2'),axis=-1) # B*n
        logits_first_null = tf.ones(tf.shape(self.logits_first))*-100
        self.logits_first = tf.where(condition=logits_first_mask,x=self.logits_first,y=logits_first_null)
        # using own prediction
        pd_first = CategoricalPdType(-1).pdfromflat(flat=self.logits_first)
        ac_first = pd_first.sample()
        mask = tf.one_hot(ac_first, depth=tf.shape(emb_node)[1], dtype=tf.bool, on_value=True, off_value=False)
        emb_first = tf.boolean_mask(emb_node, mask)
        emb_first = tf.expand_dims(emb_first,axis=1)
        # using groud truth action
        ac_first_real = self.ac_real[:, 0]
        mask_real = tf.one_hot(ac_first_real, depth=tf.shape(emb_node)[1], dtype=tf.bool, on_value=True, off_value=False)
        emb_first_real = tf.boolean_mask(emb_node, mask_real)
        emb_first_real = tf.expand_dims(emb_first_real, axis=1)

        ### 3.2: select second node
        # rules: do not select first node
        # using own prediction
        self.logits_second = tf.transpose(bilinear(emb_first, emb_node, name='logits_second'), [0, 2, 1])
        self.logits_second = tf.squeeze(self.logits_second, axis=-1)
        ac_first_mask = tf.one_hot(ac_first, depth=tf.shape(emb_node)[1], dtype=tf.bool, on_value=False, off_value=True)
        logits_second_mask = tf.logical_and(logits_mask,ac_first_mask)
        logits_second_null = tf.ones(tf.shape(self.logits_second)) * -100
        self.logits_second = tf.where(condition=logits_second_mask, x=self.logits_second, y=logits_second_null)

        pd_second = CategoricalPdType(-1).pdfromflat(flat=self.logits_second)
        ac_second = pd_second.sample()
        mask = tf.one_hot(ac_second, depth=tf.shape(emb_node)[1], dtype=tf.bool, on_value=True, off_value=False)
        emb_second = tf.boolean_mask(emb_node, mask)
        emb_second = tf.expand_dims(emb_second, axis=1)

        # using groudtruth
        self.logits_second_real = tf.transpose(bilinear(emb_first_real, emb_node, name='logits_second'), [0, 2, 1])
        self.logits_second_real = tf.squeeze(self.logits_second_real, axis=-1)
        ac_first_mask_real = tf.one_hot(ac_first_real, depth=tf.shape(emb_node)[1], dtype=tf.bool, on_value=False, off_value=True)
        logits_second_mask_real = tf.logical_and(logits_mask,ac_first_mask_real)
        self.logits_second_real = tf.where(condition=logits_second_mask_real, x=self.logits_second_real, y=logits_second_null)

        ac_second_real = self.ac_real[:,1]
        mask_real = tf.one_hot(ac_second_real, depth=tf.shape(emb_node)[1], dtype=tf.bool, on_value=True, off_value=False)
        emb_second_real = tf.boolean_mask(emb_node, mask_real)
        emb_second_real = tf.expand_dims(emb_second_real, axis=1)

        ### 3.3 predict edge type
        # using own prediction
        self.logits_edge = tf.reshape(bilinear_multi(emb_first,emb_second,out_dim=ob['adj'].get_shape()[1]),[-1,ob['adj'].get_shape()[1]])
        pd_edge = CategoricalPdType(-1).pdfromflat(self.logits_edge)
        ac_edge = pd_edge.sample()

        # using ground truth
        self.logits_edge_real = tf.reshape(bilinear_multi(emb_first_real, emb_second_real, out_dim=ob['adj'].get_shape()[1]),
                                      [-1, ob['adj'].get_shape()[1]])

        print('ob_adj', ob['adj'].get_shape(),
              'ob_node', ob['node'].get_shape())
        print('logits_first', self.logits_first.get_shape(),
              'logits_second',self.logits_second.get_shape(),
              'logits_edge', self.logits_edge.get_shape())
        print('ac_edge', ac_edge.get_shape())

        # ncat_list = [tf.shape(logits_first),ob_space['adj'].shape[-1],ob_space['adj'].shape[0]]
        self.pd = self.pdtype(-1).pdfromflat([self.logits_first,self.logits_second_real,self.logits_edge_real])
        self.vpred = tf.layers.dense(emb_node, 32, activation=tf.nn.relu, name='value1')
        self.vpred = tf.layers.dense(self.vpred, 1, activation=None, name='value2')
        self.vpred = tf.reduce_max(self.vpred,axis=1)

        self.state_in = []
        self.state_out = []

        self.ac = tf.concat((tf.expand_dims(ac_first,axis=1),tf.expand_dims(ac_second,axis=1),tf.expand_dims(ac_edge,axis=1)),axis=1)

        debug = {}
        debug['ob_node'] = tf.shape(ob['node'])
        debug['ob_adj'] = tf.shape(ob['adj'])
        debug['emb_node'] = emb_node
        debug['emb_node1'] = self.emb_node1
        debug['emb_node2'] = self.emb_node2
        debug['logits_first'] = self.logits_first
        debug['logits_second'] = self.logits_second
        debug['ob_len'] = ob_len
        debug['logits_first_mask'] = logits_first_mask
        debug['logits_second_mask'] = logits_second_mask
        # debug['pd'] = self.pd.logp(self.ac)
        debug['ac'] = self.ac

        stochastic = tf.placeholder(dtype=tf.bool, shape=())
        self._act = U.function([stochastic, ob['adj'], ob['node']], [self.ac, self.vpred, debug]) # add debug in second arg if needed

    def act(self, stochastic, ob):
        return self._act(stochastic, ob['adj'][None], ob['node'][None])
        # return self._act(stochastic, ob['adj'], ob['node'])

    def get_variables(self):
        return tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, self.scope)
    def get_trainable_variables(self):
        return tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, self.scope)
    def get_initial_state(self):
        return []






if __name__ == "__main__":
    adj_np = np.ones((5,3,4,4))
    adj = tf.placeholder(shape=(5,3,4,4),dtype=tf.float32)
    node_feature_np = np.ones((5,1,4,3))
    node_feature = tf.placeholder(shape=(5,1,4,3),dtype=tf.float32)


    ob_space = {}
    atom_type = 5
    ob_space['adj'] = gym.Space(shape=[3,5,5])
    ob_space['node'] = gym.Space(shape=[1,5,atom_type])
    ac_space = gym.spaces.MultiDiscrete([10, 10, 3])
    policy = GCNPolicy(name='policy',ob_space=ob_space,ac_space=ac_space)

    stochastic = True
    env = gym.make('molecule-v0')  # in gym format
    ob = env.reset()

    # ob['adj'] = np.repeat(ob['adj'][None],2,axis=0)
    # ob['node'] = np.repeat(ob['node'][None],2,axis=0)

    print('adj',ob['adj'].shape)
    print('node',ob['node'].shape)
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        for i in range(20):
            ob = env.reset()
            for j in range(0,20):
                ac,vpred,debug = policy.act(stochastic,ob)
                # if ac[0]==ac[1]:
                #     print('error')
                # else:
                # print('i',i,'ac',ac,'vpred',vpred,'debug',debug['logits_first'].shape,debug['logits_second'].shape)
                print('i', i)
                # print('ac\n',ac)
                # print('debug\n',debug['ob_len'])
                ob,reward,_,_ = env.step(ac)

