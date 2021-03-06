# pylint: disable=no-self-use,invalid-name

import numpy
from numpy.testing import assert_almost_equal
from keras.layers import Embedding, Input
from keras.models import Model

from deep_qa.layers.attention.matrix_attention import MatrixAttention
from deep_qa.layers.wrappers.output_mask import OutputMask

class TestMatrixAttentionLayer:
    def test_call_works_on_simple_input(self):
        sentence_1_length = 2
        sentence_2_length = 3
        embedding_dim = 3
        sentence_1_embedding = Input(shape=(sentence_1_length, embedding_dim), dtype='float32')
        sentence_2_embedding = Input(shape=(sentence_2_length, embedding_dim,), dtype='float32')
        attention_layer = MatrixAttention()
        attention = attention_layer([sentence_1_embedding, sentence_2_embedding])
        model = Model(input=[sentence_1_embedding, sentence_2_embedding], output=[attention])
        sentence_1_tensor = numpy.asarray([[[1, 1, 1], [-1, 0, 1]]])
        sentence_2_tensor = numpy.asarray([[[1, 1, 1], [-1, 0, 1], [-1, -1, -1]]])
        attention_tensor = model.predict([sentence_1_tensor, sentence_2_tensor])
        assert attention_tensor.shape == (1, sentence_1_length, sentence_2_length)
        assert_almost_equal(attention_tensor, [[[3, 0, -3], [0, 2, 0]]])

    def test_call_handles_masking_properly(self):
        sentence_length = 4
        vocab_size = 4
        embedding_dim = 3
        embedding_weights = numpy.asarray([[0, 0, 0], [1, 1, 1], [-1, 0, 1], [-1, -1, 0]])
        embedding = Embedding(vocab_size, embedding_dim, weights=[embedding_weights], mask_zero=True)
        sentence_1_input = Input(shape=(sentence_length,), dtype='int32')
        sentence_2_input = Input(shape=(sentence_length,), dtype='int32')
        sentence_1_embedding = embedding(sentence_1_input)
        sentence_2_embedding = embedding(sentence_2_input)
        attention_layer = MatrixAttention()
        attention = attention_layer([sentence_1_embedding, sentence_2_embedding])
        attention_mask = OutputMask()(attention)
        model = Model(input=[sentence_1_input, sentence_2_input], output=[attention, attention_mask])
        sentence_1_tensor = numpy.asarray([[0, 0, 1, 3]])
        sentence_2_tensor = numpy.asarray([[0, 1, 0, 2]])
        attention_tensor, attention_mask = model.predict([sentence_1_tensor, sentence_2_tensor])
        expected_attention = numpy.asarray([[[0, 0, 0, 0],
                                             [0, 0, 0, 0],
                                             [0, 3, 0, 0],
                                             [0, -2, 0, 1]]])
        expected_mask = numpy.asarray([[[0, 0, 0, 0],
                                        [0, 0, 0, 0],
                                        [0, 1, 0, 1],
                                        [0, 1, 0, 1]]])
        assert_almost_equal(attention_tensor, expected_attention)
        assert_almost_equal(attention_mask, expected_mask)
