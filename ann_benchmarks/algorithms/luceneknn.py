"""
ann-benchmarks interface for Apache Lucene.
"""

import sklearn.preprocessing
import numpy as np
from multiprocessing.pool import ThreadPool


import lucene
from lucene import JArray
from java.nio.file import Paths
from org.apache.lucene.store import FSDirectory
from org.apache.lucene.search import KnnVectorQuery, IndexSearcher
from org.apache.lucene.index import IndexWriter, IndexWriterConfig, VectorSimilarityFunction, DirectoryReader
from org.apache.lucene.codecs.lucene94 import Lucene94Codec, Lucene94HnswVectorsFormat
from org.apache.lucene.document import Document, FieldType, KnnVectorField, StoredField

from ann_benchmarks.algorithms.base import BaseANN


class Codec(Lucene94Codec):
    """
    Custom codec so that the appropriate Lucene94 codec can be returned with the configured M and efConstruction
    """
    def __init__(self, M, efConstruction):
        super(Codec, self).__init__()
        self.M = M
        self.efConstruction = efConstruction

    def getKnnVectorsFormatForField(self):
        Lucene94HnswVectorsFormat(self.M, self.efConstruction)


class PyLuceneKNN(BaseANN):
    """
    KNN using the Lucene Vector datatype.
    """

    def __init__(self, metric: str, dimension: int, param):
        try:
            lucene.initVM(vmargs=['-Djava.awt.headless=true -Xmx6g -Xms6g'])
        except ValueError:
            print(f'VM already initialized')
        self.metric = metric
        self.dimension = dimension
        self.param = param
        self.short_name = f"luceneknn-{param['M']}-{param['efConstruction']}"
        self.simFunc = VectorSimilarityFunction.DOT_PRODUCT if self.metric == "angular" \
            else VectorSimilarityFunction.EUCLIDEAN
        if self.metric not in ("euclidean", "angular"):
            raise NotImplementedError(f"Not implemented for metric {self.metric}")

    def done(self):
        if self.dir:
            self.dir.close()

    def fit(self, X):
        if self.dimension != X.shape[1]:
            raise Exception(f"Configured dimension {self.dimension} but data has shape {X.shape}")
        if self.metric == 'angular':
            X = sklearn.preprocessing.normalize(X, axis=1, norm='l2')
        iwc = IndexWriterConfig().setOpenMode(IndexWriterConfig.OpenMode.CREATE)
        codec = Codec(self.param['M'], self.param['efConstruction'])
        iwc.setCodec(codec)
        iwc.setRAMBufferSizeMB(1994.0)
        self.dir = FSDirectory.open(Paths.get(self.short_name + ".index"))
        iw = IndexWriter(self.dir, iwc)
        fieldType = KnnVectorField.createFieldType(self.dimension, self.simFunc)
        id = 0
        # X is a numpy matrix, JArray casting only works on python lists.
        X = X.tolist()
        for x in X:
            doc = Document()
            doc.add(KnnVectorField("knn", JArray('float')(x), fieldType))
            doc.add(StoredField("id", id))
            iw.addDocument(doc)
            id += 1
            if id + 1 % 1000 == 0:
                print(f"LuceneKNN: written {id} docs")
        # Force merge so only one HNSW graph is searched.
        iw.forceMerge(1)
        print(f"LuceneKNN: written {id} docs")
        iw.close()
        self.searcher = IndexSearcher(DirectoryReader.open(self.dir))

    def set_query_arguments(self, ef):
        self.name = f"luceneknn-{self.dimension}-{self.param['M']}-{self.param['efConstruction']}-{ef}"
        self.ef = ef

    def query(self, q, n):
        if self.metric == 'angular':
            q = q / np.linalg.norm(q)
        return self.run_knn_query(num_candidates=self.ef, n=n, q=q.tolist())

    def prepare_batch_query(self, X, n):
        if self.metric == 'angular':
            X = sklearn.preprocessing.normalize(X, axis=1, norm='l2')
        self.queries = X.tolist()
        self.topK = n

    def run_batch_query(self):
        self.res = []
        append = self.res.append
        n = self.topK
        num_candidates = self.ef
        for q in self.queries:
            append(self.run_knn_query(num_candidates=num_candidates, n=n, q=q))

    def get_batch_results(self):
        return self.res

    def run_knn_query(self, num_candidates, n, q):
        query = KnnVectorQuery("knn", JArray('float')(q), num_candidates)
        topdocs = self.searcher.search(query, n)
        return [int(self.searcher.doc(d.doc).get("id")) for d in topdocs.scoreDocs]

    def batch_query(self, X, n):
        pool = ThreadPool()
        if self.metric == 'angular':
            X = sklearn.preprocessing.normalize(X, axis=1, norm='l2')
        X = X.tolist()
        num_candidates = self.ef
        self.res = pool.map(lambda q: self.run_knn_query(num_candidates=num_candidates, n=n, q=q), X)
