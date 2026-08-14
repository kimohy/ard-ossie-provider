# Semantics 是州

# Marketing Insight Data Semantics

号h

左叫

Marketing Insight

2026\-08\-11

Synthetic Insights

v1\.0

# 1\.川

是Marketing Insight 叫 Al\-Ready Data Semantics \|叫\,Al Agent\|列 口

，\.

# 2\.

品号lol

Primary Key

Foreign Key

synthetic\_workspace\.marketing\_campaign

p\!ubiedwen

synthetic\_workspace\.marketing\_creative

lo

creative\_id

&#32;campaign\_id&#32;

synthetic\_workspace\.marketing\_delivery

lo

event\_date\, campaign\_id\, creative\_id

&#32;campaign\_id\, creative\_id

synthetic\_workspace\.marketing\_outcome

event\_date\, campaign\_id

campaign\_id&#32;

# 3\.

lo

碧 \(Campaign\)

\.campaign\_id\.

Initiative

marketing\_campaign

\(Creative\)

\.creative\_id\.

Variant

marketing\_creative

号亚\(Objective Type\)

Awareness\,Interest\,Action本号丑是异\.

Goal Group

marketing\_campaign

二（ChannelGroup\)

Search，Social\,Video，Display\.

Media Group

marketing\_campaign

\(MarketZone\)

North\,South\,East\,West是是\.

Zone

marketing\_campaign

\(Format Type\)

Image\, Short Video\, Text Card 亚\.

Creative Format

marketing\_creative

叫人印叶\(Message Theme\)

Discovery，Benefit\,Reminder叫人\.

Theme

marketing\_creative

\(Delivery\)

Serving

marketing\_delivery

# 3\.\(\)

lo

上今\(lmpression Count\)

Impressions

marketing\_delivery

今\(Engagement Count\)

Engagements

marketing\_delivery

合\(Interest Signal\)

Interest Event

marketing\_outcome

朝京\(Action Signal\)

Y

Outcome Signal

marketing\_outcome

7大\(ModeledValue\)

Synthetic Value

marketing\_outcome

Window\)

Window

marketing\_outcome

（Audience Cluster\)

Synthetic Cluster

marketing\_delivery

alol

&#32;Campaign

L吕 p\!ubiedwe

campaign\_id

Creative

campaign\_id&#32;

Delivery

event\_date \+ campaign\_id\+ creative\_id  1

campaign\_id\, creative\_id

event\_date \+ campaign\_id  1

campaign\_id&#32;

# 5\.\(\)

Rule

h

1

2

3

o

Eevent\_date\,campaign\_id\,creative\_id啡\.

4

o

&#32;event\_date\,campaign\_id 今否 \.

5

lol

6

今

，，，0

7

00

8

，，\.

6

10

# 6\.

\/lo

碧圳今\(Campaign Count\)

COUNT\(DISTINCT campaign\_id\)

2

marketing\_campaign\.ca mpaign\_id

\(Active Campaign Count\)

Active

COUNT\(DISTINCT campaign\_id\) WHERE campaign\_status \= Active

2

marketing\_campaign

今\(Creative Count\)

COUNT\(DISTINCT creative\_id\)

2

marketing\_creative\.crea tive\_id

上套今\(mpressions\)

SUM\(impression\_count\)

marketing\_delivery\.imp ression\_count

个\(Engagements\)

SUM\(engagement\_count\)

marketing\_delivery\.eng agement\_count

\(Engagement Rate\)

晏

SUM\(engagement\_count\) \/ NULLIF\(SUM\(impression\_count\)\,0\)

\%

marketing\_delivery

# 6\.\(\)

\/lo

\(sHun puads\)A

loY

SUM\(spend\_units\)

unit

marketing\_delivery\.spe nd\_units

ad\) Engagement\)

loYL

SUM\(spend\_units\) \/ NULLIF\(SUM\(engagement\_count\)\,0\)

unit

marketing\_delivery

台今\(Interest Signals\)

SUM\(interest\_signal\_count\)

marketing\_outcome

今\(Action Signals\)

SUM\(action\_signal\_count\)

marketing\_outcome

（Action Signal Rate\)

SUM\(action\_signal\_count\) \/ NULLIF\(SUM\(impression\_count\)\,0\)

\%

delivery \+ outcome

\(ModeledValue Units\)

SUM\(modeled\_value\_units\)

unit

marketing\_outcome

豆今\(Modeled Efficiency\)

SUM\(modeled\_value\_units\)\/ NULLIF\(SUM\(spend\_units\)\,0\)

index

delivery \+ outcome

号

LI

百

今大

互叫

DeliveryOutcome\.

# 8\.

|  |
| --- |
| 今100\%否 |
| 100\% |
| 240\|LH |
| HO |
