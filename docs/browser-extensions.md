# Browser group topics and archive known-forget checks

These endpoints use the existing `browse` service credential, current issuer origin, registered
browser account, exact actor/scope registration, current local binding and guarded user Store.
They do not create another memory service or storage table. Original browser endpoints retain
their source and request behavior. The extra packages are coordinator-pinned local candidates;
the original published user memory contracts are unchanged.

## Group topics

`POST /internal/v1/memory/browser/topics` accepts the frozen group-memory-projection request:
`scope`, `origin`, exact same-room group `subject`, `start_at`, `end_at`, `limit`, `cursor`, plus
the normal request/schema IDs. The time interval is start-inclusive and end-exclusive. It
returns latest completed segments first, bounded to one page. The signed cursor binds the
scope, subject, interval, source revision and actual scope version. A stale cursor is rejected.

Topic prose comes only from a committed segment receipt. The corresponding event, original
material IDs, complete source snapshot and saved context windows enter the existing source
barrier. The final local snapshot reuses the segment's original current-source predicate.
Each cited member's real person/actor identity, role, timestamp and reality is retained; record
refs resolve to the original exact current records. A known invalid dependency suppresses the
whole affected segment/topic instead of deleting a person's citation from the old prose.
Other authoritative denial/unavailability still fails closed. Topic IDs are local to a segment;
attribution does not assert agreement, a social edge or cross-segment topic identity. Private
associations do not broaden this projection: all source scopes must belong to this actor/group.
Portraits are not automatically bound into topic membership.

This is a projection of completed consolidation, not an archive completeness, heat or activity
counter. A page may contain no items and still have a next cursor when its scanned segments
were invalidated. No raw event, window body or write ledger is exposed.

## Archive known-forget gate

`POST /internal/v1/memory/browser/archive-check` takes `mapping_id` and up to 100 items containing
the Chat `evidence_id` locator, original `content_hash`, `sender_id` and complete seven-key
`native_alias`. The locator's final hash must match `content_hash`. The response preserves the
exact item order/locator/hash and reports `withdrawn`, `no_known_forget` or `unsupported`, with
`core`, `observation` or `unmapped`, and a current checked timestamp.

Each browser reader explicitly registers its existing actual mapping in
`browser_readers.<service>.archive_mappings.<mapping_id>`:

```json
{
  "scope": {"actor_id":"actor:registered","person_id":"person:browser","audience":"group","conversation_id":"conversation:canonical"},
  "channel": {"namespace":"qq","binding_id":"binding:actual","channel_conversation_id":"actual-channel","thread_id":null},
  "platform_id": "actual-adapter-instance",
  "self_id": "90001",
  "room_id": "20002",
  "conversation_id": "group:20002"
}
```

These are configuration placeholders for a real mapping, not values derived from a model.
Only `external` aliases can use the registered OneBot conversion. Alias account is BOT/self;
sender is the original author. QQNT IDs and mismatching self/room mappings are unsupported.
Core-only checks use the original channel/native-message source key, stored physical author,
same-actor admissions and suppression. Observation checks use the exact registered
platform/self/physical-room/author/native event and all actual material revisions. Existing
Core/observation aliases propagate either negative direction. Explicit suppression/withdrawn
state wins; natural derived TTL alone is not a source forget.

A trusted mapping with no historical Core/observation record returns
`unmapped/no_known_forget`, so real imported history remains usable. This is only a check for
known local negatives, not a Core-current proof, archive authorization or deletion from Chat.
Chat locator/hash currentness must still be checked by its owner before use. Source service
failure is not reinterpreted as `no_known_forget`. This read never creates admission, accepts
materials, invokes a model or scans all archive messages.
