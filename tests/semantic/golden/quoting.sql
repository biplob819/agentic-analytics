CREATE OR REPLACE SEMANTIC VIEW DB.SCH.MINI_SV
  TABLES (
    sales AS DB.SCH.SALES
      WITH SYNONYMS = ('deals')
      COMMENT = 'One row per sale.'
  )
  FACTS (
    sales.price AS sales.price
      WITH SYNONYMS = ('sale price')
      COMMENT = 'Price in GBP.'
  )
  DIMENSIONS (
    sales.town AS sales.town
      WITH SYNONYMS = ('city', 'buyer''s town')
      COMMENT = 'Town in uppercase, e.g. ''LONDON''. Sample values: LONDON, ST. IVES.',
    sales.date AS sales.date
      COMMENT = 'Completion date.',
    sales.year AS YEAR(sales.date)
      COMMENT = 'Calendar year.',
    sales.month AS DATE_TRUNC('MONTH', sales.date)
      COMMENT = 'First day of the month.'
  )
  METRICS (
    sales.median_price AS MEDIAN(sales.price)
      WITH SYNONYMS = ('middle price')
      COMMENT = 'Median price, GBP.',
    sales.sales_count AS COUNT(sales.price)
      COMMENT = 'Sales.'
  )
  COMMENT = 'A tiny model. It''s for tests; a back\\slash stays.';
