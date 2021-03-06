from typing import Any, Dict

from keras.layers import Dense, Input, merge
from overrides import overrides

from ...data.instances.character_span_instance import CharacterSpanInstance
from ...layers.attention.matrix_attention import MatrixAttention
from ...layers.attention.masked_softmax import MaskedSoftmax
from ...layers.attention.weighted_sum import WeightedSum
from ...layers.backend.max import Max
from ...layers.backend.repeat import Repeat
from ...layers.complex_concat import ComplexConcat
from ...layers.highway import Highway
from ...layers.wrappers.time_distributed import TimeDistributed
from ...training.text_trainer import TextTrainer
from ...training.models import DeepQaModel


class BidirectionalAttentionFlow(TextTrainer):
    """
    This class implements Minjoon Seo's `Bidirectional Attention Flow model
    <https://www.semanticscholar.org/paper/Bidirectional-Attention-Flow-for-Machine-Seo-Kembhavi/7586b7cca1deba124af80609327395e613a20e9d>`_
    for answering reading comprehension questions (ICLR 2017).

    The basic layout is pretty simple: encode words as a combination of word embeddings and a
    character-level encoder, pass the word representations through a bi-LSTM/GRU, use a matrix of
    attentions to put question information into the passage word representations (this is the only
    part that is at all non-standard), pass this through another few layers of bi-LSTMs/GRUs, and
    do a softmax over span start and span end.

    Parameters
    ----------
    num_hidden_seq2seq_layers : int, optional (default: ``2``)
        At the end of the model, we add a few stacked biLSTMs (or similar), to give the model some
        depth.  This parameter controls how many deep layers we should use.
    num_passage_words : int, optional (default: ``None``)
        If set, we will truncate (or pad) all passages to this length.  If not set, we will pad all
        passages to be the same length as the longest passage in the data.
    num_question_words : int, optional (default: ``None``)
        Same as ``num_passage_words``, but for the number of words in the question.  (default:
        ``None``)
    num_highway_layers : int, optional (default: ``2``)
        After constructing a word embedding, but before the first biLSTM layer, Min has some
        ``Highway`` layers operating on the word embedding layer.  This parameter specifies how
        many of those to do.  (default: ``2``)
    highway_activation : string, optional (default: ``'relu'``)
        Specifies the activation function to use for the ``Highway`` layers mentioned above.  Any
        Keras activation function is acceptable here.
    similarity_function : Dict[str, Any], optional (default: ``{'type': 'linear', 'combination': 'x,y,x*y'}``)
        Specifies the similarity function to use when computing a similarity matrix between
        question words and passage words.  By default we use the function Min used in his paper.

    Notes
    -----
    Min's code uses tensors of shape ``(batch_size, num_sentences, sentence_length)`` to represent
    the passage, splitting it up into sentences, where here we just have one long passage sequence.
    I was originally afraid this might mean he applied the biLSTM on each sentence independently,
    but it looks like he flattens it to our shape before he does any actual operations on it.  So,
    I `think` this is implementing pretty much exactly what he did, but I'm not totally certain.
    """
    def __init__(self, params: Dict[str, Any]):
        # There are a couple of defaults from TextTrainer that we want to override: we want to
        # default to using joint word and character-level embeddings, and we want to use a CNN
        # encoder to get a character-level encoding.  We set those here.
        params.setdefault('tokenizer', {'type': 'words and characters'})
        encoder_params = params.pop('encoder', {'default': {}})
        encoder_params.setdefault('word', {'type': 'cnn', 'ngram_filter_sizes': [5], 'num_filters': 100})
        params['encoder'] = encoder_params
        self.num_hidden_seq2seq_layers = params.pop('num_hidden_seq2seq_layers', 2)
        self.num_passage_words = params.pop('num_passage_words', None)
        self.num_question_words = params.pop('num_question_words', None)
        self.num_highway_layers = params.pop('num_highway_layers', 2)
        self.highway_activation = params.pop('highway_activation', 'relu')
        self.similarity_function_params = params.pop('similarity_function',
                                                     {'type': 'linear', 'combination': 'x,y,x*y'})
        # We have two outputs, so using "val_acc" doesn't work.
        params.setdefault('validation_metric', 'val_loss')
        super(BidirectionalAttentionFlow, self).__init__(params)

    @overrides
    def _build_model(self):
        # PART 1:
        # First we create input layers and pass the inputs through an embedding.

        question_input = Input(shape=self._get_sentence_shape(self.num_question_words),
                               dtype='int32', name="question_input")
        passage_input = Input(shape=self._get_sentence_shape(self.num_passage_words),
                              dtype='int32', name="passage_input")
        # Shape: (batch_size, num_question_words, embedding_size * 2) (embedding_size * 2 because,
        # by default in this class, self._embed_input concatenates a word embedding with a
        # character-level encoder).
        question_embedding = self._embed_input(question_input)

        # Shape: (batch_size, num_passage_words, embedding_size * 2)
        passage_embedding = self._embed_input(passage_input)

        # Min's model has some highway layers here, with relu activations.  Note that highway
        # layers don't change the tensor's shape.  We need to have two different `TimeDistributed`
        # layers instantiated here, because Keras doesn't like it if a single `TimeDistributed`
        # layer gets applied to two inputs with different numbers of time steps.
        for i in range(self.num_highway_layers):
            highway_layer = Highway(activation=self.highway_activation, name='highway_{}'.format(i))
            question_layer = TimeDistributed(highway_layer, name=highway_layer.name + "_qtd")
            question_embedding = question_layer(question_embedding)
            passage_layer = TimeDistributed(highway_layer, name=highway_layer.name + "_ptd")
            passage_embedding = passage_layer(passage_embedding)

        # Then we pass the question and passage through a seq2seq encoder (like a biLSTM).  This
        # essentially pushes phrase-level information into the embeddings of each word.
        phrase_layer = self._get_seq2seq_encoder(name="phrase",
                                                 fallback_behavior="use default params")

        # Shape: (batch_size, num_question_words, embedding_size * 2)
        encoded_question = phrase_layer(question_embedding)

        # Shape: (batch_size, num_passage_words, embedding_size * 2)
        encoded_passage = phrase_layer(passage_embedding)

        # PART 2:
        # Now we compute a similarity between the passage words and the question words, and
        # normalize the matrix in a couple of different ways for input into some more layers.
        matrix_attention_layer = MatrixAttention(similarity_function=self.similarity_function_params,
                                                 name='passage_question_similarity')
        # Shape: (batch_size, num_passage_words, num_question_words)
        passage_question_similarity = matrix_attention_layer([encoded_passage, encoded_question])

        # Shape: (batch_size, num_passage_words, num_question_words), normalized over question
        # words for each passage word.
        passage_question_attention = MaskedSoftmax()(passage_question_similarity)
        # Shape: (batch_size, num_passage_words, embedding_size * 2)
        passage_question_vectors = WeightedSum()([encoded_question, passage_question_attention])

        # Min's paper finds, for each document word, the most similar question word to it, and
        # computes a single attention over the whole document using these max similarities.
        # Shape: (batch_size, num_passage_words)
        question_passage_similarity = Max(axis=-1)(passage_question_similarity)
        # Shape: (batch_size, num_passage_words)
        question_passage_attention = MaskedSoftmax()(question_passage_similarity)
        # Shape: (batch_size, embedding_size * 2)
        question_passage_vector = WeightedSum()([encoded_passage, question_passage_attention])

        # Then he repeats this question/passage vector for every word in the passage, and uses it
        # as an additional input to the hidden layers above.
        repeat_layer = Repeat(axis=1, repetitions=self.num_passage_words)
        # Shape: (batch_size, num_passage_words, embedding_size * 2)
        tiled_question_passage_vector = repeat_layer(question_passage_vector)

        # Shape: (batch_size, num_passage_words, embedding_size * 8)
        final_merged_passage = ComplexConcat(combination='1,2,1*2,1*3')([encoded_passage,
                                                                         passage_question_vectors,
                                                                         tiled_question_passage_vector])

        # PART 3:
        # Having computed a combined representation of the document that includes attended question
        # vectors, we'll pass this through a few more bi-directional encoder layers, then predict
        # the span_begin word.  Hard to find a good name for this; Min calls this part of the
        # network the "modeling layer", so we'll call this the `modeled_passage`.
        modeled_passage = final_merged_passage
        for i in range(self.num_hidden_seq2seq_layers):
            hidden_layer = self._get_seq2seq_encoder(name="hidden_seq2seq_{}".format(i),
                                                     fallback_behavior="use default params")
            modeled_passage = hidden_layer(modeled_passage)

        # To predict the span word, we pass the merged representation through a Dense layer without
        # output size 1 (basically a dot product of a vector of weights and the passage vectors),
        # then do a softmax to get a position.
        span_begin_input = merge([final_merged_passage, modeled_passage], mode='concat')
        span_begin_weights = TimeDistributed(Dense(output_dim=1))(span_begin_input)
        # Shape: (batch_size, num_passage_words)
        span_begin_probabilities = MaskedSoftmax(name="span_begin_softmax")(span_begin_weights)

        # PART 4:
        # Given what we predicted for span_begin, we'll pass it through a final encoder layer and
        # predict span_end.  NOTE: I'm following what Min did in his _code_, not what it says he
        # did in his _paper_.  The equations in his paper do not mention that he did this last
        # weighted passage representation and concatenation before doing the final biLSTM (though
        # his figure makes it clear this is what he intended; he just wrote the equations wrong).
        # Shape: (batch_size, num_passage_words, embedding_size * 2)
        passage_weighted_by_predicted_span = repeat_layer(WeightedSum()([modeled_passage,
                                                                         span_begin_probabilities]))
        span_end_representation = ComplexConcat(combination="1,2,3,2*3")([final_merged_passage,
                                                                          modeled_passage,
                                                                          passage_weighted_by_predicted_span])
        final_seq2seq = self._get_seq2seq_encoder(name="final_seq2seq",
                                                  fallback_behavior="use default params")
        span_end_representation = final_seq2seq(span_end_representation)
        span_end_input = merge([final_merged_passage, span_end_representation], mode='concat')
        span_end_weights = TimeDistributed(Dense(output_dim=1))(span_end_input)
        span_end_probabilities = MaskedSoftmax(name="span_end_softmax")(span_end_weights)

        return DeepQaModel(input=[question_input, passage_input],
                           output=[span_begin_probabilities, span_end_probabilities])

    def _instance_type(self):  # pylint: disable=no-self-use
        return CharacterSpanInstance

    @overrides
    def _get_max_lengths(self) -> Dict[str, int]:
        max_lengths = super(BidirectionalAttentionFlow, self)._get_max_lengths()
        max_lengths['num_passage_words'] = self.num_passage_words
        max_lengths['num_question_words'] = self.num_question_words
        return max_lengths

    @overrides
    def _set_max_lengths(self, max_lengths: Dict[str, int]):
        # Adding this because we're bypassing word_sequence_length in our model, but TextTrainer
        # expects it.
        max_lengths['word_sequence_length'] = None
        super(BidirectionalAttentionFlow, self)._set_max_lengths(max_lengths)
        self.num_passage_words = max_lengths['num_passage_words']
        self.num_question_words = max_lengths['num_question_words']

    @overrides
    def _set_max_lengths_from_model(self):
        self.max_sentence_length = self.model.get_input_shape_at(0)[1]
        # TODO(matt): implement this correctly
