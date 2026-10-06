"""Tiny helper used to exercise the /apply flow end to end."""


def add_tag(tag, tags=[]):
    tags.append(tag)
    return tags


def average(values):
    return sum(values) / len(values)
