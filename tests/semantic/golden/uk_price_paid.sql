CREATE OR REPLACE SEMANTIC VIEW ANALYTICS.UK.UK_PRICE_PAID_SEMANTIC
  TABLES (
    uk_price_paid AS ANALYTICS.UK.UK_PRICE_PAID
      WITH SYNONYMS = ('price paid data', 'property sales', 'house sales')
      COMMENT = 'One row per residential property sale (a price paid transaction) registered with HM Land Registry in England and Wales.'
  )
  FACTS (
    uk_price_paid.price AS uk_price_paid.price
      WITH SYNONYMS = ('sale price', 'price paid', 'amount paid')
      COMMENT = 'The price paid for the property in pounds sterling (GBP), in whole pounds, as stated on the transfer deed.'
  )
  DIMENSIONS (
    uk_price_paid.town AS uk_price_paid.town
      WITH SYNONYMS = ('city', 'post town')
      COMMENT = 'The post town of the property, in uppercase (for example ''LONDON''); compare with uppercase text. A post town is not the same as the local authority district. Sample values: LONDON, MANCHESTER, BIRMINGHAM, LEEDS, BRISTOL.',
    uk_price_paid.district AS uk_price_paid.district
      WITH SYNONYMS = ('borough', 'local authority', 'council area')
      COMMENT = 'The local authority district (the council area) the property is in, in uppercase; compare with uppercase text.',
    uk_price_paid.county AS uk_price_paid.county
      COMMENT = 'The county the property is in, in uppercase; compare with uppercase text. Large urban areas such as GREATER LONDON and GREATER MANCHESTER appear as counties. Sample values: GREATER LONDON, GREATER MANCHESTER, WEST MIDLANDS, WEST YORKSHIRE.',
    uk_price_paid.locality AS uk_price_paid.locality
      WITH SYNONYMS = ('village', 'neighbourhood')
      COMMENT = 'The locality (a village or neighbourhood) within the town, in uppercase; often an empty string when not recorded.',
    uk_price_paid.street AS uk_price_paid.street
      WITH SYNONYMS = ('road', 'street name')
      COMMENT = 'The street name of the property''s address, in uppercase; an empty string when not recorded.',
    uk_price_paid.postcode1 AS uk_price_paid.postcode1
      WITH SYNONYMS = ('outward code', 'outcode')
      COMMENT = 'The outward part of the postcode, before the space (for example ''SW1A'' in ''SW1A 1AA''); an empty string when the sale has no postcode.',
    uk_price_paid.postcode2 AS uk_price_paid.postcode2
      WITH SYNONYMS = ('inward code', 'incode')
      COMMENT = 'The inward part of the postcode, after the space (for example ''1AA'' in ''SW1A 1AA''); an empty string when the sale has no postcode.',
    uk_price_paid.type AS uk_price_paid.type
      WITH SYNONYMS = ('property type', 'house type', 'kind of property')
      COMMENT = 'The property type, one of five lowercase values: terraced, semi-detached, detached, flat (flats and maisonettes) or other. Do not rely on the sort order of these values. Sample values: terraced, semi-detached, detached, flat, other.',
    uk_price_paid.duration AS uk_price_paid.duration
      WITH SYNONYMS = ('tenure', 'freehold or leasehold')
      COMMENT = 'The tenure of the property, one of three lowercase values: freehold, leasehold or unknown. Do not rely on the sort order of these values. Sample values: freehold, leasehold, unknown.',
    uk_price_paid.is_new AS uk_price_paid.is_new
      WITH SYNONYMS = ('new build', 'newly built', 'new home')
      COMMENT = 'Whether the property was newly built when sold: 1 for a new build, 0 for an established building. It is a number, not a boolean, so filter with is_new = 1 or is_new = 0. Sample values: 1, 0.',
    uk_price_paid.date AS uk_price_paid.date
      WITH SYNONYMS = ('sale date', 'completion date', 'transaction date')
      COMMENT = 'The date the sale completed (the completion date on the transfer deed), not the date it was registered. The data starts on 1 January 1995; do not count sales after the as-of date.',
    uk_price_paid.year AS YEAR(uk_price_paid.date)
      WITH SYNONYMS = ('sale year')
      COMMENT = 'The calendar year of the completion date, as a number such as 2019.',
    uk_price_paid.month AS DATE_TRUNC('MONTH', uk_price_paid.date)
      WITH SYNONYMS = ('sale month')
      COMMENT = 'The calendar month of the completion date, given as the first day of that month.'
  )
  METRICS (
    uk_price_paid.sales_count AS COUNT(uk_price_paid.price)
      WITH SYNONYMS = ('number of sales', 'transactions', 'sales volume')
      COMMENT = 'The number of sales; each row is one sale.',
    uk_price_paid.total_value AS SUM(uk_price_paid.price)
      WITH SYNONYMS = ('total sales value', 'total spend')
      COMMENT = 'The sum of the prices paid, in GBP.',
    uk_price_paid.average_price AS AVG(uk_price_paid.price)
      WITH SYNONYMS = ('mean price', 'avg price')
      COMMENT = 'The mean price paid, in GBP.',
    uk_price_paid.median_price AS MEDIAN(uk_price_paid.price)
      WITH SYNONYMS = ('middle price', 'median sale price')
      COMMENT = 'The median (middle) price paid, in GBP, computed exactly rather than approximated.'
  )
  COMMENT = 'Residential property sales in England and Wales from HM Land Registry''s Price Paid Data, one row per sale, from 1 January 1995 onwards. Prices are in pounds sterling (GBP). Place names (town, district, county, locality, street) are stored in uppercase, so compare them with uppercase text such as ''LONDON''. Missing text values are empty strings, not NULL. Every question comes with an as-of date, the last day of the month before the latest month in the data: treat it as today and do not count sales after it. Sales appear only after they are registered, weeks to months after completion, so the last few months before the as-of date are under-counted and keep filling in with later releases. Repossessions and buy-to-let sales (category B) are included and cannot be told apart from standard sales.';
