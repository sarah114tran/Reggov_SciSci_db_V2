-- final_rfi_cleanup_v2.sql
-- Removes the 12 remaining RFI self-reference citations from
-- comment_citations (the other 396 of the reviewed 408 were already
-- removed historically). IDs are hardcoded literally -- no CSV import,
-- no temp table, no subquery -- after the previous version silently
-- deleted every row due to a sqlite3 .import/column-resolution bug.
--
-- Run from the db/ directory:
--   sqlite3 dockets.db < final_rfi_cleanup_v2.sql
--
-- A backup (dockets.db.bak_pre_final_rfi_cleanup) already exists from
-- before this was first attempted.

SELECT 'comment_citations before:', COUNT(*) FROM comment_citations;

DELETE FROM comment_citations WHERE citation_id IN (
  '278caa06-0588-59b1-84b4-6f3e85436e43',
  '3a434ea6-d3e0-5aaf-95c6-6eeb2848e09f',
  '4608af3a-552a-5906-97d3-05c38fa97663',
  '49801c94-7a38-5cf4-ba4b-cf96a782c486',
  '5faaeb80-342f-5492-ac37-3e826438f543',
  '6f45b118-f3c5-53fb-9419-c548f83d0018',
  '8c110c01-47e4-5e74-9ae8-29142001f6ff',
  'df24af0d-1c77-5e87-85af-46ba01ec965a',
  'e09a5041-da15-590a-bbed-83b9a7d39e13',
  'f4cde31d-27e6-5151-8a49-0d4f311ca93e',
  'f7cc5321-603d-5da2-bc4f-db2db65b8bb2',
  'fe02871a-21d3-5355-9787-8ada8a90e8d9'
);

SELECT 'comment_citations after (expect exactly 12 fewer):', COUNT(*) FROM comment_citations;
