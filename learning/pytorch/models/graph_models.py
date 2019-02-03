import sys
import os
sys.path.append(os.path.join(os.environ['ITHEMAL_HOME'], 'learning', 'pytorch'))

from enum import Enum
import torch
import torch.nn as nn
import torch.nn.functional as F
import common_libs.utilities as ut
import data.data_cost as dt
import torch.autograd as autograd
import torch.optim as optim
import math
import numpy as np
from typing import Any, Dict, List, NamedTuple, Optional, Union, Tuple
from . import model_utils

class AbstractGraphModule(nn.Module):

    def __init__(self, embedding_size, hidden_size, num_classes):
        # type: (int, int, int) -> None
        super(AbstractGraphModule, self).__init__()

        self.embedding_size = embedding_size
        self.num_classes = num_classes
        self.hidden_size = hidden_size

    def set_learnable_embedding(self, mode, dictsize, seed = None):
        # type: (str, int, Optional[int]) -> None

        self.mode = mode

        if mode != 'learnt':
            embedding = nn.Embedding(dictsize, self.embedding_size)

        if mode == 'none':
            print 'learn embeddings form scratch...'
            initrange = 0.5 / self.embedding_size
            embedding.weight.data.uniform_(-initrange, initrange)
            self.final_embeddings = embedding
        elif mode == 'seed':
            print 'seed by word2vec vectors....'
            embedding.weight.data = torch.FloatTensor(seed)
            self.final_embeddings = embedding
        elif mode == 'learnt':
            print 'using learnt word2vec embeddings...'
            self.final_embeddings = seed
        else:
            print 'embedding not selected...'
            exit()

    def dump_shared_params(self):
        # type: () -> Dict[str, Any]
        return model_utils.dump_shared_params(self)

    def load_shared_params(self, params):
        # type: (Dict[str, Any]) -> None
        model_utils.load_shared_params(self, params)

    def init_hidden(self):
        # type: () -> Tuple[nn.Parameter, nn.Parameter]

        return (
            nn.Parameter(torch.zeros(1, 1, self.hidden_size, requires_grad=True)),
            nn.Parameter(torch.zeros(1, 1, self.hidden_size, requires_grad=True)),
        )

    def remove_refs(self, item):
        # type: (dt.DataItem) -> None
        pass

class GraphNN(AbstractGraphModule):

    def __init__(self, embedding_size, hidden_size, num_classes, use_residual=True, linear_embed=False, use_dag_rnn=True):
        # type: (int, int, int, bool, bool, bool) -> None
        super(GraphNN, self).__init__(embedding_size, hidden_size, num_classes)

        assert use_residual or use_dag_rnn, 'Must use some type of predictor'

        self.use_residual = use_residual
        self.linear_embed = linear_embed
        self.use_dag_rnn = use_dag_rnn

        #lstm - input size, hidden size, num layers
        self.lstm_token = nn.LSTM(self.embedding_size, self.hidden_size)
        self.lstm_ins = nn.LSTM(self.hidden_size, self.hidden_size)

        # linear weight for instruction embedding
        self.opcode_lin = nn.Linear(self.embedding_size, self.hidden_size)
        self.src_lin = nn.Linear(self.embedding_size, self.hidden_size)
        self.dst_lin = nn.Linear(self.embedding_size, self.hidden_size)
        # for sequential model
        self.opcode_lin_seq = nn.Linear(self.embedding_size, self.hidden_size)
        self.src_lin_seq = nn.Linear(self.embedding_size, self.hidden_size)
        self.dst_lin_seq = nn.Linear(self.embedding_size, self.hidden_size)

        #linear layer for final regression result
        self.linear = nn.Linear(self.hidden_size,self.num_classes)

        #lstm - for sequential model
        self.lstm_token_seq = nn.LSTM(self.embedding_size, self.hidden_size)
        self.lstm_ins_seq = nn.LSTM(self.hidden_size, self.hidden_size)
        self.linear_seq = nn.Linear(self.hidden_size, self.num_classes)

    def remove_refs(self, item):
        # type: (dt.DataItem) -> None

       for instr in item.block.instrs:
            if instr.lstm != None:
                del instr.lstm
            if instr.hidden != None:
                del instr.hidden
            instr.lstm = None
            instr.hidden = None
            instr.tokens = None

    def init_bblstm(self, item):
        # type: (dt.DataItem) -> None

        self.remove_refs(item)
        for i, instr in enumerate(item.block.instrs):
            tokens = item.x[i]
            if self.mode == 'learnt':
                instr.tokens = [self.final_embeddings[token] for token in tokens]
            else:
                instr.tokens = self.final_embeddings(torch.LongTensor(tokens))

    def reduction(self, v1, v2):
        # type: (torch.tensor, torch.tensor) -> torch.tensor
        return torch.max(v1,v2)

    def create_graphlstm(self, block):
        # type: (ut.BasicBlock) -> torch.tensor

        leaves = block.find_leaves()

        leaf_hidden = []
        for leaf in leaves:
            hidden = self.create_graphlstm_rec(leaf)
            leaf_hidden.append(hidden[0].squeeze())

        final_hidden = leaf_hidden[0]

        for hidden in leaf_hidden[1:]:
            final_hidden = self.reduction(final_hidden, hidden)

        return final_hidden

    def get_instruction_embedding_linear(self, instr, seq_model):
        # type: (ut.Instruction, bool) -> torch.tensor

        if seq_model:
            opcode_lin = self.opcode_lin_seq
            src_lin = self.src_lin_seq
            dst_lin = self.dst_lin_seq
        else:
            opcode_lin = self.opcode_lin
            src_lin = self.src_lin
            dst_lin = self.dst_lin

        opc_embed = instr.tokens[0]
        src_embed = instr.tokens[2:2+len(instr.srcs)]
        dst_embed = instr.tokens[-1-len(instr.dsts):-1]

        opc_hidden = opcode_lin(opc_embed)

        src_hidden = torch.zeros(self.embedding_size)
        for s in src_embed:
            src_hidden = torch.max(F.relu(src_lin(s)))

        dst_hidden = torch.zeros(self.embedding_size)
        for d in dst_embed:
            dst_hidden = torch.max(F.relu(dst_lin(d)))

        return (opc_hidden + src_hidden + dst_hidden).unsqueeze(0).unsqueeze(0)


    def get_instruction_embedding_lstm(self, instr, seq_model):
        # type: (ut.Instruction, bool) -> torch.tensor
        if seq_model:
            lstm = self.lstm_token_seq
        else:
            lstm = self.lstm_token

        _, hidden = lstm(instr.tokens.unsqueeze(1), self.init_hidden())
        return hidden[0]

    def get_instruction_embedding(self, instr, seq_model):
        # type: (ut.Instruction, bool) -> torch.tensor
        if self.linear_embed:
            return self.get_instruction_embedding_linear(instr, seq_model)
        else:
            return self.get_instruction_embedding_lstm(instr, seq_model)

    def create_graphlstm_rec(self, instr):
        # type: (ut.Instruction) -> torch.tensor

        if instr.hidden != None:
            return instr.hidden

        parent_hidden = []
        for parent in instr.parents:
            hidden = self.create_graphlstm_rec(parent)
            parent_hidden.append(hidden)

        in_hidden_ins = self.init_hidden()
        if len(parent_hidden) > 0:
            in_hidden_ins = parent_hidden[0]
        h = in_hidden_ins[0]
        c = in_hidden_ins[1]
        for hidden in parent_hidden:
            h = self.reduction(h,hidden[0])
            c = self.reduction(c,hidden[1])
        in_hidden_ins = (h,c)

        ins_embed = self.get_instruction_embedding(instr, False)

        out_ins, hidden_ins = self.lstm_ins(ins_embed, in_hidden_ins)
        instr.hidden = hidden_ins

        return instr.hidden

    def create_residual_lstm(self, block):
        # type: (ut.BasicBlock) -> torch.tensor

        ins_embeds = autograd.Variable(torch.zeros(len(block.instrs),self.embedding_size))
        for i, ins in enumerate(block.instrs):
            ins_embeds[i] = self.get_instruction_embedding(ins, True).squeeze()

        ins_embeds_lstm = ins_embeds.unsqueeze(1)

        _, hidden_ins = self.lstm_ins_seq(ins_embeds_lstm, self.init_hidden())

        seq_ret = hidden_ins[0].squeeze()

        return seq_ret


    def forward(self, item):
        # type: (dt.DataItem) -> torch.tensor

        self.init_bblstm(item)

        final_pred = torch.zeros(self.num_classes).squeeze()

        if self.use_dag_rnn:
            graph = self.create_graphlstm(item.block)
            final_pred += self.linear(graph).squeeze()

        if self.use_residual:
            sequential = self.create_residual_lstm(item.block)
            final_pred += self.linear_seq(sequential).squeeze()

        return final_pred.squeeze()

class RnnHierarchyType(Enum):
    NONE = 0
    DENSE =  1
    MULTISCALE = 2
    LINEAR_MODEL = 3
    MOP_MODEL = 4

class RnnType(Enum):
    RNN = 0
    LSTM = 1
    GRU = 2

RnnParameters = NamedTuple('RnnParameters', [
    ('embedding_size', int),
    ('hidden_size', int),
    ('num_classes', int),
    ('connect_tokens', bool),
    ('skip_connections', bool),
    ('learn_init', bool),
    ('hierarchy_type', RnnHierarchyType),
    ('rnn_type', RnnType),
])


class RNN(AbstractGraphModule):

    def __init__(self, params):
        # type: (RnnParameters) -> None
        super(RNN, self).__init__(params.embedding_size, params.hidden_size, params.num_classes)

        self.params = params

        if params.rnn_type == RnnType.RNN:
            self.token_rnn = nn.RNN(self.embedding_size, self.hidden_size)
            self.instr_rnn = nn.RNN(self.hidden_size, self.hidden_size)
        elif params.rnn_type == RnnType.LSTM:
            self.token_rnn = nn.LSTM(self.embedding_size, self.hidden_size)
            self.instr_rnn = nn.LSTM(self.hidden_size, self.hidden_size)
        elif params.rnn_type == RnnType.GRU:
            self.token_rnn = nn.GRU(self.embedding_size, self.hidden_size)
            self.instr_rnn = nn.GRU(self.hidden_size, self.hidden_size)
        else:
            raise ValueError('Unknown RNN type {}'.format(params.rnn_type))

        self._token_init = self.rnn_init_hidden()
        self._instr_init = self.rnn_init_hidden()

        self.linear = nn.Linear(self.hidden_size, self.num_classes)

    def rnn_init_hidden(self):
        # type: () -> Union[Tuple[nn.Parameter, nn.Parameter], nn.Parameter]

        hidden = self.init_hidden()
        for h in hidden:
            torch.nn.init.kaiming_uniform_(h)

        if self.params.rnn_type == RnnType.LSTM:
            return hidden
        else:
            return hidden[0]

    def get_token_init(self):
        # type: () -> torch.tensor
        if self.params.learn_init:
            return self._token_init
        else:
            return self.rnn_init_hidden()

    def get_instr_init(self):
        # type: () -> torch.tensor
        if self.params.learn_init:
            return self._instr_init
        else:
            return self.rnn_init_hidden()

    def pred_of_instr_chain(self, instr_chain):
        # type: (torch.tensor) -> torch.tensor
        _, final_state_packed = self.instr_rnn(instr_chain, self.get_instr_init())
        if self.params.rnn_type == RnnType.LSTM:
            final_state = final_state_packed[0]
        else:
            final_state = final_state_packed

        return self.linear(final_state.squeeze()).squeeze()


    def forward(self, item):
        # type: (dt.DataItem) -> torch.tensor

        token_state = self.get_token_init()

        token_output_map = {} # type: Dict[ut.Instruction, torch.tensor]
        token_state_map = {} # type: Dict[ut.Instruction, torch.tensor]

        for instr, token_inputs in zip(item.block.instrs, item.x):
            if not self.params.connect_tokens:
                token_state = self.get_token_init()

            if self.params.skip_connections and self.params.hierarchy_type == RnnHierarchyType.NONE:
                for parent in instr.parents:
                    parent_state = token_state_map[parent]

                    if self.params.rnn_type == RnnType.LSTM:
                        token_state = (
                            token_state[0] + parent_state[0],
                            token_state[1] + parent_state[1],
                        )
                    else:
                        token_state = token_state + parent_state

            tokens = self.final_embeddings(torch.LongTensor(token_inputs)).unsqueeze(1)
            output, state = self.token_rnn(tokens, token_state)
            token_output_map[instr] = output
            token_state_map[instr] = state

        if self.params.hierarchy_type == RnnHierarchyType.NONE:
            final_state_packed = token_state_map[item.block.instrs[-1]]

            if self.params.rnn_type == RnnType.LSTM:
                final_state = final_state_packed[0]
            else:
                final_state = final_state_packed
            return self.linear(final_state.squeeze()).squeeze()

        instr_chain = torch.stack([token_output_map[instr][-1] for instr in item.block.instrs])

        if self.params.hierarchy_type == RnnHierarchyType.DENSE:
            instr_chain = torch.stack([state for instr in item.block.instrs for state in token_output_map[instr]])
        elif self.params.hierarchy_type == RnnHierarchyType.LINEAR_MODEL:
            return sum(
                self.linear(st).squeeze()
                for st in instr_chain
            )
        elif self.params.hierarchy_type == RnnHierarchyType.MOP_MODEL:
            preds = torch.stack([
                self.pred_of_instr_chain(torch.stack([token_output_map[instr][-1] for instr in instrs]))
                for instrs in item.block.paths_of_block()
            ])
            return torch.max(preds)

        return self.pred_of_instr_chain(instr_chain)
