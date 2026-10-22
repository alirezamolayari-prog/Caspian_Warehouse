"""Link to Caspian Server: goods requests the owners approve on their phones.

The mall has no fixed IP, so this app always calls the server, never the other way round:
`sync.SyncEngine` sends what waits in the outbox (`ServerOutbox`) and asks `/changes` for decisions
and messages. Everything is saved locally first, so registering a request never waits for the
network. An approved request becomes a draft issue voucher (`issue_link`) that keeps the approved
lines; printing it posts it and reports «issued» back to the server.
"""
