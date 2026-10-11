"""Shared contract between Person A (control plane) and Person B (execution plane).

This package is the *frozen interface*: Mongo document shapes, collection names,
job-queue protocol, enums, ID conventions, and content hashing. Both halves of
Airlock build against it. Nothing here executes untrusted code or owns task
authority; it only describes the data both sides exchange through Atlas.
"""
