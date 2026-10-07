---
title: Adding credit
description: How to add credit to your account while self-service payment is unavailable.
---

**Self-service payment is not available yet.** Credit is added manually by our team. Contact them using either method below and they will process it.

## Contact

| Method | Detail |
|---|---|
| Phone / WeChat | see your account manager |
| Email | sales@example.invalid |

Support hours are Monday to Friday, 09:00–18:00 China Standard Time (UTC+8).

## Process

1. **Get in touch.** Send your account details and the amount you want to add.
2. **Confirm the amount** with the team before paying.
3. **Pay** using the instructions they provide. WeChat Pay, Alipay, and bank transfer are all accepted.
4. **Tell them once you have paid**, so the payment can be matched to your account.
5. **Check your balance** in the console, or with `GET /api/v1/account/balance`.

## Before you pay

- **Confirm which account the credit is for.** One organisation may hold more than one account, and a payment applied to the wrong one has to be reversed manually.
- **Keep the payment receipt.** It is what the team matches against if a payment does not appear.

## Until the credit lands

A balance of zero or less means job submissions return **402 `insufficient_balance`**. Jobs already running are unaffected and finish normally. See [Billing and usage](../billing/).
