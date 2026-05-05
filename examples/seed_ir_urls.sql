-- Seed canonical IR URLs into tracked_companies.ir_url for the 28-name book.
-- Verify each URL before running — corporate IR sites do change paths.
-- Apply with:  sqlite3 data/portfolio.db < data/seed_ir_urls.sql

-- Portfolio (12)
UPDATE tracked_companies SET ir_url = 'https://ir.aboutamazon.com'                         WHERE ticker = 'AMZN';
UPDATE tracked_companies SET ir_url = 'https://abc.xyz/investor'                            WHERE ticker = 'GOOG';
UPDATE tracked_companies SET ir_url = 'https://investor.mercadolibre.com'                   WHERE ticker = 'MELI';
UPDATE tracked_companies SET ir_url = 'https://investor.atmeta.com'                         WHERE ticker = 'META';
UPDATE tracked_companies SET ir_url = 'https://ir.veeva.com'                                WHERE ticker = 'VEEV';
UPDATE tracked_companies SET ir_url = 'https://www.servicenow.com/company/investors.html'   WHERE ticker = 'NOW';
UPDATE tracked_companies SET ir_url = 'https://investors.wix.com'                           WHERE ticker = 'WIX';
UPDATE tracked_companies SET ir_url = 'https://www.novonordisk.com/investors.html'          WHERE ticker = 'NVO';
UPDATE tracked_companies SET ir_url = 'https://ir.rubrik.com'                               WHERE ticker = 'RBRK';
UPDATE tracked_companies SET ir_url = 'https://investors.nu'                                WHERE ticker = 'NU';
UPDATE tracked_companies SET ir_url = 'https://bn.brookfield.com'                           WHERE ticker = 'BN';
UPDATE tracked_companies SET ir_url = 'https://www.franklintempleton.com/investments/options/exchange-traded-funds' WHERE ticker = 'FLKR';

-- Watchlist (16)
UPDATE tracked_companies SET ir_url = 'https://investor.lilly.com'                          WHERE ticker = 'LLY';
UPDATE tracked_companies SET ir_url = 'https://ir.appliedmaterials.com'                     WHERE ticker = 'AMAT';
UPDATE tracked_companies SET ir_url = 'https://investors.fcx.com'                           WHERE ticker = 'FCX';
UPDATE tracked_companies SET ir_url = 'https://investor.weyerhaeuser.com'                   WHERE ticker = 'WY';
UPDATE tracked_companies SET ir_url = 'https://www.jpmorganchase.com/ir'                    WHERE ticker = 'JPM';
UPDATE tracked_companies SET ir_url = 'https://investors.tollbrothers.com'                  WHERE ticker = 'TOL';
UPDATE tracked_companies SET ir_url = 'https://www.asml.com/en/investors'                   WHERE ticker = 'ASML';
UPDATE tracked_companies SET ir_url = 'https://www.bhp.com/investors'                       WHERE ticker = 'BHP';
UPDATE tracked_companies SET ir_url = 'https://www.riotinto.com/invest'                     WHERE ticker = 'RIO';
UPDATE tracked_companies SET ir_url = 'https://vale.com/investors'                          WHERE ticker = 'VALE';
UPDATE tracked_companies SET ir_url = 'https://investors.airbnb.com'                        WHERE ticker = 'ABNB';
UPDATE tracked_companies SET ir_url = 'https://investors.sofi.com'                          WHERE ticker = 'SOFI';
UPDATE tracked_companies SET ir_url = 'https://investor.lemonade.com'                       WHERE ticker = 'LMND';
UPDATE tracked_companies SET ir_url = 'https://www.hdfcbank.com/personal/about-us/investor-relations' WHERE ticker = 'HDB';
UPDATE tracked_companies SET ir_url = 'https://www.franco-nevada.com/investors'             WHERE ticker = 'FNV';
UPDATE tracked_companies SET ir_url = 'https://www.cnrl.com/investors'                      WHERE ticker = 'CNQ';

-- Quick check: verify all 28 are populated
SELECT ticker, ir_url FROM tracked_companies WHERE instrument_type IS NOT NULL AND ir_url IS NULL;
-- (should return zero rows)
