"""Word statistics."""
from collections import Counter


def words(text):
    """Split text into words."""
    return text.split()


def word_counts(text):
    return Counter(words(text))


def top_words(text, n=10):
    """The n most common words as (word, count) pairs."""
    return word_counts(text).most_common(n)
