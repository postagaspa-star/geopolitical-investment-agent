-- ============================================================================
-- apply_cash_delta — mutazione ATOMICA del cash_balance del portafoglio
-- ============================================================================
-- Risolve la race read-modify-write (#4 audit): prima ogni trade leggeva il
-- cash, calcolava il nuovo valore assoluto e lo riscriveva in step separati →
-- due writer concorrenti (pipeline schedulata, auto-exit del polling ogni 60s,
-- chat decisionale via HTTP, adjust-cash) si sovrascrivevano a vicenda,
-- CREANDO o DISTRUGGENDO denaro sul portafoglio.
--
-- Questa RPC fa l'UPDATE in un'unica istruzione atomica (cash = cash + delta)
-- e ritorna il nuovo saldo. Il backend (db_supabase.apply_cash_delta) la chiama
-- al posto di update_portfolio per il cash; anche il rollback usa il delta
-- inverso, così non riscrive mai un valore assoluto stale.
--
-- ESEGUIRE UNA VOLTA nel SQL Editor del progetto Supabase.
-- ============================================================================

create or replace function public.apply_cash_delta(p_delta numeric)
returns numeric
language plpgsql
as $$
declare
  new_balance numeric;
begin
  update public.portfolio
     set cash_balance = cash_balance + p_delta,
         updated_at   = now()
   where id = (select max(id) from public.portfolio)
   returning cash_balance into new_balance;

  return new_balance;
end;
$$;
