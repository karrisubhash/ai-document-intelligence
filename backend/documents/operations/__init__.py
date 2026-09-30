"""Operational read models: audit trail, processing monitor, system status.

Everything here is administrative and scoped by accounts.permissions - a
department administrator sees their own department's slice, a system
administrator sees all of it. Nothing here returns document CONTENT; the
audit trail and the job table were built never to hold any.
"""
