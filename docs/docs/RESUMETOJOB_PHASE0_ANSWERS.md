# Answers to the resumetojob Phase 0 plan

Paste everything below the line into the resumetojob session. Source for each answer: the Interviewer service code,
its tests and `docs/docs/openapi.json`. "Jack decided" marks a product decision.

---

Thank you, the plan is approved with the answers and changes below. Your corrections to my prompt were right: Stripe
event dedupe exists, `delete_account` is the hook, the hash helper does not exist, `RTJ_` is the prefix, and the
prompt's plan table was stale (fixed in the Interviewer repo, PR #21). Phase 1 may start after you read this.

## Answers to your questions

1. **Who can buy what?** Jack decided: **any signed-in user** can buy the add-ons, subscriber or not. The service does not
   check, so the app needs no subscription gate. `economy` and `premium` are **wallets, not products**: there is no
   "Economy $10" or "Premium $20" Price. Only the five add-on skus are sold, so create **five** Prices:
   `addon_5` ($5), `addon_10_economy` ($10), `addon_10_premium` ($10), `addon_20_economy` ($20), `addon_20_premium`
   ($20). Read names and sizes from `GET /plans` (`skus`), not from the app.
2. **Which wallet does a start request use?** `GET /packs/{ref}` returns **all wallets in one response**, one entry per
   plan whether active or not, each with `minutes_remaining`, `days_remaining`, `refund_eligible` and
   `allowed_durations`. Included wallets allow 15 minutes only; `economy` and `premium` allow 15, 20 and 25. If the
   student wants 20 or 25 minutes, fall through to a bought wallet that allows it and has enough minutes, or say so.
   Rule: among wallets that allow the length and have enough minutes, spend the one that **expires soonest**, and prefer an
   included wallet on a tie. **Always send `plan` on create**; if omitted the service uses `economy`. The service
   enforces length (422) and minutes (402), so a wrong choice fails safely.
3. **Does `/packs/activate` with a sku work for a student with no wallet?** Yes. The wallet is created on first purchase
   (covered by tests).
4. **Portal plan change (weekly to monthly, same subscription id).** Do **not** expire the old included wallet on the
   switch; let it run out on its own date, and grant the new plan on its first paid invoice. When the subscription
   really ends (`customer.subscription.deleted`), call `/packs/expire` for **all three** included plans
   (`included_weekly`, `included_monthly`, `included_quarterly`); it is a harmless no-op for plans the student never
   had. No `customer.subscription.updated` rule is needed.
5. **Stripe Prices.** The subscription Prices already exist. Create the **five add-on Prices in test mode yourself**
   with a script, recording ids in `RTJ_STRIPE_PRICE_ID_VOICE_*`. Do not create anything in live mode: Jack does that
   later, by hand. There are no Economy/Premium plan Prices (see answer 1).
6. **Erasure and unused packs.** Jack decided: wallets are **deleted with the account**. The service gets
   `DELETE /api/v1/packs/by-ref/{external_ref}` (Interviewer PR #22; it removes every wallet and purchase record for a
   student, is idempotent, and returns `{"deleted_wallets": N, "deleted_payments": M}`). In `delete_account`, after
   cancelling the Stripe subscription: `DELETE /interviews/by-ref/{ref}`, then `DELETE /packs/by-ref/{ref}`, then
   delete the user. Any failure aborts and the user retries, as for your existing "billing" stage. The confirmation
   dialog names the minutes that will be lost; no automatic refund. Stripe keeps the financial record, so no counsel
   question remains about a surviving hashed reference.
7. **Retention.** Jack decided **90 days**, counted from the interview's creation, applied by the service
   (`RETENTION_DAYS=90`). Wallets are not affected. Show "reports are kept for 90 days"; after that the history row keeps
   score and readiness.
8. **Staging hosts.** Jack decided on a **separate resumetojob staging app and database, with Stripe test mode**. Jack
   will create the app and tell you its public host; he will add that host to the Interviewer staging service's
   `CALLBACK_ALLOWED_HOSTS` and set the secrets himself. Until then, develop against your fake service and local tests.
   Staging service URL: `http://interviewer-ms-staging.flycast` (plain http, port 80, private network only).
9. **Branching.** Yes: a clean worktree from `origin/main`. Do not mix with the unrelated billing-checkout-guard work.
10. **OpenAPI.** `docs/docs/openapi.json` in the Interviewer repo (PR #21), kept in sync by a test. Generate the client
    types and the fake service from it. Jack will attach the file; if you cannot see it, ask.

## Changes and additions to your plan

- **Approved as written:** external_ref (full 64 hex HMAC, own pepper, never rotates); `RTJ_INTERVIEWER_*` names;
  `mode=payment` Checkout for add-ons with the PaymentIntent id as `payment_id`; `invoice.paid` grants for included
  plans (amount above 0, reason create or cycle) with the invoice id; ordering guard on expire; consent row with the
  provider list and the re-consent rule; minimal stored report data; migrations 041 onward; the two-PR split of Phase 1;
  ADR-0016 and the CLAUDE.md section 11 edit.
- **Double start.** `POST /interviews` has no idempotency key; two requests book two interviews and debit twice. Guard it
  in the app: a button lock plus a server-side rule of one active interview per student at a time, checked in a
  transaction.
- **Wallet display.** Show every wallet (included and bought) with minutes, interviews left (`floor(minutes/15)`) and days.
- **Long requests.** The service's AI timeout is 40 s per provider, and creating an interview can take 10 to 30 s (rarely
  longer). Keep your client timeout at 120 s or more. Verify empirically in Phase 5 whether Fly's proxy or Vercel cuts
  browser requests that last more than about 60 s; if anything does, change start and answer to "accept, then poll".
- **Tenant limits.** The service allows 300 AI-backed calls a minute and 20,000 a day for all students together. Handle 429.
- **Do not trust my reading of the contract.** Where the OpenAPI file and the prompt disagree, the OpenAPI file wins; tell
  me what differed.

Start Phase 1. Open pull requests only; do not merge, deploy, run `fly secrets set` with real values, or use Stripe
live mode.
