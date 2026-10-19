-- Tombstone ids follow the SPEC 4.5 id grammar: `del_<ULID>` (they were written as `tomb_<ULID>`). Existing rows keep
-- their ULID, so keyset pagination of `/v1/tombstones` (`starting_after`) stays in creation order across the rename.
update tombstones set id = 'del_' || substr(id, 6) where id like 'tomb\_%';
