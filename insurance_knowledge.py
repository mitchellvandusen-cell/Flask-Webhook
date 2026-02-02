# insurance_knowledge.py
# Deep product knowledge for the sales bot.
# NO example sentences. Only explanations and understanding.
# The LLM uses this as reference to formulate its own questions and responses.

POLICY_KNOWLEDGE = """
=== LIFE INSURANCE PRODUCT KNOWLEDGE ===

You have deep knowledge of every type of life insurance. Use this understanding to ask sharp, revealing questions that expose gaps in someone's current coverage or help them understand what they actually need. Never lecture. Never dump information. Use this knowledge to guide your questions so the answers reveal the truth of their situation.


--- TERM LIFE INSURANCE ---

What it is: Pure death benefit protection for a set period. 10, 15, 20, or 30 year terms are most common. If you die during the term, your beneficiary gets the payout. If the term expires and you are still alive, the policy ends with zero value.

Strengths: Cheapest way to get a large death benefit. Simple and easy to understand. Good for covering a specific financial obligation like a mortgage or income replacement during working years.

Problems and gaps:
When the term ends, the coverage is gone. There is no cash value, nothing built up, nothing to show for years of premiums. Renewal after the term expires is astronomically more expensive because you are now older. If your health changed during the term, you may not qualify for a new policy at all. So someone who bought a 20-year term at 35 is now 55 with no coverage and possibly uninsurable. Term only makes sense as part of a larger strategy, not as someone's only coverage. Many people treat it as their entire plan and get caught off guard when it expires.

Age and pricing reality:
Under 40, term is very affordable for large coverage amounts. 40 to 50, still reasonable but premiums climb. 50 to 60, gets noticeably expensive, especially for 20 or 30 year terms. 60 and older, term becomes extremely expensive for meaningful coverage. A 58 year old looking at a 10 year term is going to pay significantly more than they expect. At 65 plus, term is often not the right product at all.


--- WHOLE LIFE INSURANCE ---

What it is: Permanent coverage that lasts your entire life with a guaranteed cash value component. Fixed premiums that never increase. The cash value grows at a guaranteed rate and can be borrowed against.

Strengths: Lifetime coverage as long as premiums are paid. Guaranteed cash value growth. Fixed premiums locked in at the age you purchase. Can borrow against the cash value for emergencies or opportunities. Participating policies pay dividends from the carrier. Predictable and stable.

Problems and gaps:
Much more expensive than term for the same death benefit. Cash value growth is slow in the early years. Less flexible than universal life products. Returns on cash value are lower than market investments. Some people buy whole life when they actually need a larger death benefit and would be better served by term or a blended approach. The key question is whether someone is paying for a death benefit they need or a savings vehicle they do not need.


--- UNIVERSAL LIFE (UL) ---

What it is: Permanent coverage with flexible premiums and an interest-based cash value component. The policyholder can adjust premium payments and death benefit within certain limits.

Strengths: Flexibility in how much you pay and when. Adjustable death benefit. Can be a good tool for estate planning.

Problems and gaps:
This is where a lot of people get burned. UL policies from the 1980s and 1990s were sold with projected interest rates of 8 to 12 percent. Those rates never materialized long term. Many of these policies are now imploding. The policyholder gets a letter saying their cash value is depleted and they need to pay significantly more in premiums or the policy lapses. People who thought they were covered for life suddenly find out at 70 or 75 that their policy is about to collapse. If someone says they have a universal life policy, especially one that is 15 or more years old, there is a real chance it is underfunded and at risk. They may not even know it.


--- INDEXED UNIVERSAL LIFE (IUL) ---

What it is: Permanent coverage where the cash value is tied to a market index like the S&P 500 but with a guaranteed floor, usually zero percent. You get some upside when the market goes up without losing money when it goes down.

Strengths: Growth potential above what whole life offers. Downside protection with the floor. Tax advantaged cash value growth. Can supplement retirement income. Often includes living benefits which allow access to the death benefit while still alive if diagnosed with critical, chronic, or terminal illness.

Problems and gaps:
There are caps on returns so you do not get the full market upside. Internal charges and fees reduce real returns. Illustrations shown during the sale may be more optimistic than reality. It is a complex product and many people who own one do not fully understand how it works.

Age and pricing reality:
Under 50, IUL can be a powerful long term wealth building and protection tool because there is time for cash value to compound. 50 to 60, still useful but the time horizon is shorter and premiums are higher. 60 and older, IUL becomes very expensive and there is not enough time for the cash value to grow meaningfully. The premiums at 60 plus are going to be a couple hundred dollars a month regardless of health.


--- FINAL EXPENSE / BURIAL INSURANCE ---

What it is: Small whole life policies typically ranging from 1,000 to 50,000 dollars in coverage. Designed specifically to cover funeral costs, burial, medical bills, and other end of life expenses so the family is not left with that financial burden.

Strengths: Easier to qualify for than traditional life insurance. Simplified underwriting or guaranteed issue options available. Usually no medical exam required. Permanent coverage that does not expire. Affordable for what it covers. Specifically designed for the need it serves.

Problems and gaps:
The death benefit is small compared to traditional policies. The cost per thousand dollars of coverage is higher than traditional whole life. Some final expense policies are guaranteed issue which means they come with a two year waiting period. During that waiting period, if the insured dies, the beneficiary only receives a return of premiums paid plus interest, not the full death benefit. Many people do not realize this when they sign up.

Age and pricing reality:
This is the realistic option for people 60 and older who need coverage and are on a budget. But even final expense is not cheap at older ages. A 40,000 dollar final expense policy at age 60 plus is going to cost over 100 dollars a month. At 70 plus, even smaller amounts get expensive. The average funeral in the US costs between 7,000 and 12,000 dollars or more, and that does not include outstanding medical bills, debts, or other expenses the family might face.


--- GROUP / EMPLOYER LIFE INSURANCE ---

What it is: Coverage provided through an employer, typically as part of a benefits package. Usually offers 1 to 2 times annual salary in death benefit. Often free or heavily subsidized.

Strengths: Low cost or free. Easy enrollment, often automatic. No medical underwriting required to get the base coverage.

Problems and gaps:
This is the coverage type with the most hidden problems. Most people who say they are covered through work do not understand the limitations.

It ends when you leave the job, get laid off, retire, or the company changes benefits providers. It is not portable. You do not own it, your employer does. The coverage amount is almost always far below what a family actually needs. Financial planning guidelines suggest 10 to 12 times annual income for adequate life insurance. 1 to 2 times salary does not come close.

It almost never includes living benefits. Living benefits allow the policy owner to access a portion of the death benefit while still alive if they are diagnosed with a critical illness like cancer or heart attack, a chronic illness that prevents them from performing daily activities, or a terminal illness. Without living benefits, the policy only pays when you are dead. It does nothing for you while you are alive and suffering.

Conversion from group to individual coverage at retirement is technically possible with some policies but the rates are based on your age at the time of conversion, not when you originally enrolled. So someone who had free group coverage from age 25 to 65 now has to buy individual coverage at 65 year old rates. The premiums are usually shocking and unaffordable for most retirees. Many people discover this gap too late.

Group coverage gives people a false sense of security. They check the box that says they have life insurance and never think about it again until they need it and realize it is nowhere near enough.


--- GUARANTEED ISSUE LIFE INSURANCE ---

What it is: Coverage that requires no health questions, no medical exam, and guarantees acceptance regardless of health status. Usually final expense sized policies from 5,000 to 25,000 dollars.

Strengths: Anyone can get it regardless of health conditions. Simple application process. Permanent coverage.

Problems and gaps:
Very expensive per dollar of coverage compared to any underwritten product. Low death benefit amounts. The biggest problem is the two year waiting period that most guaranteed issue policies include. If the insured dies within the first two years, the beneficiary does NOT receive the full death benefit. They only get a return of premiums paid plus a small amount of interest. This is frequently not explained clearly during the sale. The carriers that market these most aggressively on television and radio, such as Globe Life and Colonial Penn, rely on people not understanding this limitation. Many people who could qualify for a medically underwritten policy with no waiting period and lower premiums end up in guaranteed issue products because they assume they cannot qualify for anything better. They never actually applied for a real policy to find out.


--- VARIABLE LIFE INSURANCE ---

What it is: Permanent coverage where the cash value is invested in market sub-accounts similar to mutual funds. The policyholder chooses the investments.

Strengths: Potential for higher returns than other permanent products. Investment control and flexibility.

Problems and gaps:
The cash value is subject to full market risk. If investments perform poorly, the cash value can decrease. High internal fees and charges. Complex product that requires active management. If the investments lose enough value, additional premiums may be required to keep the policy in force. Not suitable for someone who is not comfortable with investment risk inside their life insurance.


=== CRITICAL SELLING POINTS AND KNOWLEDGE ===


--- LIVING BENEFITS ---
This is one of the most powerful differentiators in modern life insurance and most people have never heard of it.

Living benefits are riders, usually included at no additional cost in newer policies, that allow the policy owner to access a portion of the death benefit while the insured is still alive under specific qualifying conditions.

Three types:
Critical illness: Access to funds if diagnosed with a qualifying critical illness such as heart attack, stroke, invasive cancer, organ transplant, or similar conditions.
Chronic illness: Access to funds if the insured is unable to perform two or more of the six activities of daily living (bathing, dressing, toileting, transferring, continence, eating) or has a cognitive impairment requiring substantial supervision.
Terminal illness: Access to funds if diagnosed with a terminal illness with a life expectancy of 12 to 24 months depending on the carrier.

Why this matters enormously: the number one cause of bankruptcy in the United States is medical bills. A traditional life insurance policy without living benefits only helps your family AFTER you die. With living benefits, the policy can help YOU while you are alive and dealing with a major health crisis. You can use the funds for treatment, to replace income while you cannot work, to modify your home for accessibility, or anything else. It is your money.

Most group employer policies do NOT have living benefits. Most older policies do NOT have them. This is a massive gap that most people are not aware of.


--- THE AGE AND COST REALITY ---

The single most important thing to understand about life insurance pricing: every year you wait, it gets more expensive and your options narrow.

Someone in their 20s or 30s can get substantial coverage for very little money. Every type of product is affordable and available. This is the ideal time to lock in rates and build cash value.

In the 40s, premiums start climbing but meaningful coverage is still accessible. Health conditions start playing a bigger role in underwriting.

In the 50s, everything gets noticeably more expensive. Large term policies cost real money. Permanent products require significant monthly premiums.

At 60 and older, the options narrow significantly. Term insurance for meaningful amounts is often prohibitively expensive. IUL and whole life premiums are hundreds of dollars per month. The most realistic and affordable option for many people 60 plus is final expense coverage in the 1,000 to 50,000 dollar range, and even that costs over 100 dollars a month for 40,000 in coverage. Budget constraints are very real at this age.

Someone at 60 plus should not be told that coverage is going to be super affordable or that there are tons of great options. That is dishonest. The conversation at 60 plus is about what is realistic for their budget and what specific need they are trying to address, whether that is funeral expenses, leaving something for their family, or covering outstanding debts.

The urgency lever is real and not a scare tactic: the longer someone waits, the more it costs and the fewer options they qualify for. Health can change at any time. Once you have a diagnosis, your options shrink dramatically overnight.


--- POLICY REVIEW ANGLES ---

When someone says they have coverage, there are multiple angles to explore that reveal whether their coverage actually does what they think it does:

Is it through their employer or is it their own individual policy. If employer, does it follow them if they leave. How much coverage do they actually have and is that amount enough to replace their income for their family. Does their policy include living benefits or does it only pay out at death. When was the last time they reviewed their coverage to make sure it still matches their current life situation. Has their family grown, have they taken on a mortgage, have they had children since they got the policy. If it is a universal life policy, do they know whether the cash value is still on track or if it is underfunded. If it is term, when does it expire and what is their plan after that. Is there a waiting period on their current policy. Are they paying for guaranteed issue when they might qualify for something better through actual underwriting. If they are approaching retirement, do they have a plan for what happens to their coverage when they leave their job.

None of these are accusatory. They are genuine curiosity about whether someone's coverage actually does what they think it does. Most people cannot answer more than one or two of these questions, which reveals the gap without you having to point it out.
"""
