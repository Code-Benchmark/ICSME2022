"""Functions to help with sampling trees."""

import pickle
import numpy as np
import random
import torch
from gensim.models.word2vec import Word2Vec
def gen_samples(root,trees):
    """Creates a generator that returns a tree in BFS order with each node
    replaced by its vector embedding, and a child lookup table."""
    allnodes =[]
    allchildren = []
    word2vec = Word2Vec.load(root+"/node_wiz_token_w2v_128").wv
    in2en = word2vec.index_to_key
    # encode labels as one-hot vectors
    ###
    # labels = list(set(labels))
    # label_lookup = {label: _onehot(i, len(labels)) for i, label in enumerate(labels)}
    for tree in trees:
        nodes = []
        children = []
        # label = label_lookup[tree['label']]
        id = int(tree['id'])
        queue = [(tree['tree'], -1)]
        while queue:
            node, parent_ind = queue.pop(0)
            node_ind = len(nodes)
            # add children and the parent index to the queue
            queue.extend([(child, node_ind) for child in node['children']])
            # create a list to store this node's children indices
            children.append([])
            # add this child to its parent's child list
            if parent_ind > -1:
                children[parent_ind].append(node_ind)
            if node['node'] == 'root':
                node['node'] = 'FileAST'
            if node['node'] in in2en:
                nodes.append(in2en.index(node['node']))
            else :
                nodes.append(in2en.index('UNK'))

        allnodes.append(nodes)
        allchildren.append(children)

    return allnodes, allchildren

def getid(ids,allnodes, allchildren):
    nodes, children = [], []
    n, c= allnodes, allchildren

    nodes = n[ids]
    children = c[ids]

    return nodes, children

def get_batch(allnodes, allchildren,idx,batch_size,data):
    # allnodes, allchildren, allabels = gen
    node1,node2,ch1,ch2, label= [],[],[],[],[]
    tmp = data.iloc[idx:idx+batch_size]
    for _, t in tmp.iterrows():
        nodes, children = getid(int(t['id1']), allnodes, allchildren)
        node1.append(nodes)
        ch1.append(children)

        nodes, children = getid(int(t['id2']), allnodes, allchildren)
        node2.append(nodes)
        ch2.append(children)

        label.append(int(t['label']))
    node1, ch1 = _pad_batch(node1, ch1)
    node2, ch2 = _pad_batch(node2, ch2)
    return node1,node2,ch1,ch2, label



def batch_samples(gen,ids,batch_size,data):
    """Batch samples from a generator"""
    nodes, children, labels = [], [], []
    samples = 0

    for n, c, l in gen:
        nodes.append(n)
        children.append(c)
        labels.append(l)
        samples += 1
        if samples >= batch_size:
            yield _pad_batch(nodes, children, labels)
            nodes, children, labels = [], [], []
            samples = 0

    if nodes:
        yield _pad_batch(nodes, children, labels)

def _pad_batch(nodes, children):
    if not nodes:
        return [], [], []
    max_nodes = max([len(x) for x in nodes])
    max_children = max([len(x) for x in children])
    feature_len = len(nodes[0])
    child_len = max([len(c) for n in children for c in n])

    nodes = [n + [[0] * feature_len] * (max_nodes - len(n)) for n in nodes]
    # pad batches so that every batch has the same number of nodes
    children = [n + ([[]] * (max_children - len(n))) for n in children]
    # pad every child sample so every node has the same number of children
    children = [[c + [0] * (child_len - len(c)) for c in sample] for sample in children]

    return nodes, children

def _onehot(i, total):
    return [1.0 if j == i else 0.0 for j in range(total)]
