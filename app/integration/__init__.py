"""Integration contract v1: the only channel between the institution app and
the borrower app. Outbox + dispatcher for events we send, inbound endpoint +
inbox for events we receive. Schemas and signing come from ficium-integration.
"""
