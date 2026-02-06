#!/usr/bin/env python3
"""Quick script to check account status for all your email addresses"""

import sys
sys.path.insert(0, '/home/user/Flask-Webhook')

from db import get_db_connection, return_db_connection

def check_accounts():
    conn = get_db_connection()
    if not conn:
        print("❌ Failed to connect to database")
        return

    cur = conn.cursor()

    emails = [
        'mitchvandusenlife@gmail.com',
        'mitchell_vandusen@hotmail.com',
        'mitchell.vandusen@gmail.com'
    ]

    print("="*80)
    print("CHECKING SUBSCRIBERS TABLE")
    print("="*80)

    for email in emails:
        cur.execute("""
            SELECT
                email,
                password_hash IS NOT NULL as has_password,
                role,
                stripe_customer_id,
                location_id,
                oauth_app_type
            FROM subscribers
            WHERE LOWER(email) = LOWER(%s)
        """, (email,))
        row = cur.fetchone()

        if row:
            print(f"\n✅ {email}")
            print(f"   Has Password:        {row['has_password']}")
            print(f"   Role:                {row['role']}")
            print(f"   Stripe Customer ID:  {row['stripe_customer_id'] or 'None'}")
            print(f"   Location ID:         {row['location_id']}")
            print(f"   OAuth App Type:      {row['oauth_app_type']}")
        else:
            print(f"\n❌ {email} - NOT FOUND in subscribers")

    print("\n" + "="*80)
    print("CHECKING AGENCY_BILLING TABLE")
    print("="*80)

    for email in emails:
        cur.execute("""
            SELECT
                agency_email,
                password_hash IS NOT NULL as has_password,
                subscription_tier,
                location_id,
                stripe_customer_id,
                oauth_app_type
            FROM agency_billing
            WHERE LOWER(agency_email) = LOWER(%s)
        """, (email,))
        row = cur.fetchone()

        if row:
            print(f"\n✅ {email}")
            print(f"   Has Password:        {row['has_password']}")
            print(f"   Subscription Tier:   {row['subscription_tier']}")
            print(f"   Stripe Customer ID:  {row['stripe_customer_id'] or 'None'}")
            print(f"   Location ID:         {row['location_id']}")
            print(f"   OAuth App Type:      {row['oauth_app_type']}")
        else:
            print(f"\n❌ {email} - NOT FOUND in agency_billing")

    print("\n" + "="*80)

    cur.close()
    return_db_connection(conn)

if __name__ == '__main__':
    check_accounts()
