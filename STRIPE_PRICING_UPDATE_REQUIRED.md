# 🚨 STRIPE PRICING UPDATE REQUIRED

The code has been updated to reflect new pricing, but you must **manually update your Stripe products** to match.

## Current Pricing (OLD - in Stripe)
- Individual: **$100/month**
- Agency Starter: **$800/month**
- Agency Pro: **$1600/month**

## New Pricing (UPDATED - in code)
- Individual: **$98.99/month**
- Agency Starter: **$797.99/month**
- Agency Pro: **$1597.99/month**

---

## Step-by-Step Instructions

### 1. Log into Stripe Dashboard
Go to: https://dashboard.stripe.com/

### 2. Update Individual Plan ($98.99/month)
1. Navigate to **Products** → Find your Individual plan product
2. Click **Add another price**
3. Set price to **$98.99**
4. Set billing period to **Monthly**
5. Set billing cycle to **Recurring**
6. Click **Add price**
7. **Copy the new Price ID** (looks like `price_xxxxxxxxxxxxx`)
8. Update your `.env` file:
   ```
   STRIPE_PRICE_ID=price_xxxxxxxxxxxxx
   ```

### 3. Update Agency Starter Plan ($797.99/month)
1. Navigate to **Products** → Find your Agency Starter product
2. Click **Add another price**
3. Set price to **$797.99**
4. Set billing period to **Monthly**
5. Set billing cycle to **Recurring**
6. Click **Add price**
7. **Copy the new Price ID**
8. Update your `.env` file:
   ```
   STRIPE_AGENCY_STARTER_PRICE_ID=price_xxxxxxxxxxxxx
   ```

### 4. Update Agency Pro Plan ($1,597.99/month)
1. Navigate to **Products** → Find your Agency Pro product
2. Click **Add another price**
3. Set price to **$1,597.99**
4. Set billing period to **Monthly**
5. Set billing cycle to **Recurring**
6. Click **Add price**
7. **Copy the new Price ID**
8. Update your `.env` file:
   ```
   STRIPE_AGENCY_PRO_PRICE_ID=price_xxxxxxxxxxxxx
   ```

### 5. Deploy Environment Variable Updates
After updating your `.env` file with the new price IDs:

#### If using Railway:
1. Go to your Railway project
2. Navigate to **Variables** tab
3. Update the three price ID variables
4. Railway will automatically redeploy

#### If using other hosting:
- Redeploy your application with the updated environment variables

---

## Important Notes

⚠️ **DO NOT delete the old prices in Stripe** - existing subscribers may still be on the old pricing tiers.

✅ New customers will automatically get the new pricing once you update the environment variables.

✅ The website now shows the updated pricing ($98.99, $797.99, $1597.99) in all locations.

✅ The "Subscribe Now" button in the hero section now scrolls to the pricing section instead of going directly to checkout, allowing users to see all options first.

---

## Verification Checklist

- [ ] Created new $98.99/month price in Stripe (Individual)
- [ ] Created new $797.99/month price in Stripe (Agency Starter)
- [ ] Created new $1,597.99/month price in Stripe (Agency Pro)
- [ ] Updated STRIPE_PRICE_ID in environment variables
- [ ] Updated STRIPE_AGENCY_STARTER_PRICE_ID in environment variables
- [ ] Updated STRIPE_AGENCY_PRO_PRICE_ID in environment variables
- [ ] Redeployed application with new environment variables
- [ ] Tested checkout flow to confirm new prices are showing

---

## Testing the Update

After deploying the environment variable changes:

1. Visit your website at `insurancegrokbot.click`
2. Click "Subscribe Now" - should scroll to pricing section
3. Click "Start Individual" - Stripe checkout should show **$98.99/month**
4. Go back and click "Start Agency Starter" - Should show **$797.99/month**
5. Go back and click "Start Agency Pro" - Should show **$1,597.99/month**

If the old prices still appear in Stripe checkout, double-check that you updated the environment variables correctly.
