"""Cheap Korean-friendly retrieval before bounded AI event confirmation."""
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass

STOPWORDS = {'속보', '단독', '영상', '포토', '기자', '뉴스', '오늘', '관련', '대한', '이번'}


def features(title, body=''):
    def extract(text, weight):
        text = unicodedata.normalize('NFKC', text).lower()
        text = re.sub(r'\[[^\]]*\]|【[^】]*】', ' ', text)
        for word in re.findall(r'[가-힣ぁ-ゟ゠-ヿ㐀-鿿a-z0-9]+', text):
            if word in STOPWORDS or len(word) < 2:
                continue
            yield 'w:' + word, weight * 2
            for n in (2, 3):
                for i in range(len(word) - n + 1):
                    yield f'{n}:' + word[i:i+n], weight
    result = Counter()
    for key, value in (*extract(title, 1.0), *extract(body[:300], .2)):
        # Repeated boilerplate must not dominate retrieval.
        result[key] = max(result[key], value)
    return result


@dataclass
class Candidate:
    cluster: str
    anchor: object
    evidence: object
    score: float


class CandidateIndex:
    def __init__(self):
        self.docs = {}
        self.postings = defaultdict(set)
        self.anchors = {}

    def add(self, article, cluster, anchor, search_title=None):
        key = article.id
        if key in self.docs:
            return
        vector = features(search_title or article.title, article.text if search_title in (None, article.title) else '')
        self.docs[key] = (article, cluster, vector)
        self.anchors[cluster] = anchor
        for term in vector:
            self.postings[term].add(key)

    def remove_cluster(self, cluster):
        for key, (_, cid, vector) in list(self.docs.items()):
            if cid == cluster:
                for term in vector:
                    self.postings[term].discard(key)
                del self.docs[key]
        self.anchors.pop(cluster, None)

    def search(self, article, top_k=6, min_score=.08, search_title=None):
        vector = features(search_title or article.title, article.text if search_title in (None, article.title) else '')
        size = len(self.docs)
        if not size or not vector:
            return []
        idf = {term: min(3.0, 1 + math.log((size + 1) / (len(self.postings.get(term, ())) + 1))) for term in vector}
        norm = math.sqrt(sum((value * idf[term]) ** 2 for term, value in vector.items()))
        hits = set()
        for term in vector:
            hits.update(self.postings.get(term, ()))
        groups = {}
        for key in hits:
            other, cluster, ov = self.docs[key]
            dot = sum(value * ov.get(term, 0) * idf[term] ** 2 for term, value in vector.items())
            other_norm = math.sqrt(sum((value * min(3.0, 1 + math.log((size + 1) / (len(self.postings[term]) + 1)))) ** 2 for term, value in ov.items()))
            score = dot / (norm * other_norm) if norm and other_norm else 0
            if score >= min_score and (cluster not in groups or score > groups[cluster].score):
                groups[cluster] = Candidate(cluster, self.anchors[cluster], other, score)
        # Rank distinct events, not duplicate reports from the same event.
        return sorted(groups.values(), key=lambda c: (-c.score, c.cluster))[:top_k]
