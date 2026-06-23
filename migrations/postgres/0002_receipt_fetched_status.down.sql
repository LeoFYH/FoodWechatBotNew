update production_receipts
set status = 'confirmed',
    updated_at = now()
where status = 'fetched';

alter table production_receipts
    drop constraint if exists production_receipts_status_check;

alter table production_receipts
    add constraint production_receipts_status_check
    check (status in ('confirmed', 'cancelled'));
