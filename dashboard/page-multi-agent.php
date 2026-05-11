<?php
/**
 * Template Name: Multi-Agent Tracker — Demo
 * Template Post Type: page
 *
 * Demo страница multi-agent productivity tracker'а — те же ops-panel
 * patterns что в page-dashboard.php (Mission Control). Mock data inline.
 *
 * Bilingual (ru default, en через ?lang=en — i18n.js handles toggle),
 * с конверсией $ → ₽ по курсу ЦБ РФ (cached 12h).
 *
 * @package androman
 */

if ( ! defined( 'ABSPATH' ) ) exit;

/**
 * Get USD/RUB rate from Russian Central Bank, cached 12h.
 * Fallback: 95.0 если API недоступен.
 */
function androman_ma_usd_rub_rate() {
    $rate = get_transient( 'androman_ma_usd_rub' );
    if ( $rate !== false ) return (float) $rate;

    $response = wp_remote_get( 'https://www.cbr-xml-daily.ru/daily_json.js', array( 'timeout' => 5 ) );
    if ( is_wp_error( $response ) ) return 95.0;

    $body = wp_remote_retrieve_body( $response );
    $data = json_decode( $body, true );
    $rate = isset( $data['Valute']['USD']['Value'] ) ? (float) $data['Valute']['USD']['Value'] : 95.0;

    set_transient( 'androman_ma_usd_rub', $rate, 12 * HOUR_IN_SECONDS );
    return $rate;
}

$rate = androman_ma_usd_rub_rate();

// Mock data
$totals = array(
    'calls'            => 79716,
    'cost_usd'         => 60299.41,
    'savings_usd'      => 59886.08,
    'subscription_usd' => 413.33,
    'days'             => 62,
    'period_start'     => '2026-03-09',
    'period_end'       => '2026-05-09',
);

$providers = array(
    'anthropic_claude' => array(
        'calls'    => 79715,
        'cost_usd' => 60299.21,
    ),
    'openai_codex' => array(
        'calls'    => 1,
        'cost_usd' => 0.20,
    ),
    'openrouter_openclaw' => array(
        'calls'    => 0,
        'cost_usd' => 0.00,
    ),
    'opencode_openrouter' => array(
        'calls'    => 0,
        'cost_usd' => 0.00,
    ),
);

$combined = array(
    'savings_usd'      => $totals['savings_usd'],
    'cost_usd'         => $totals['cost_usd'],
    'subscription_usd' => $totals['subscription_usd'],
);

// hours_saved = active × (multiplier − 1) = 216.6 × 6.312 ≈ 1367.2
$productivity = array(
    'active_hours'     => 216.6,
    'calendar_hours'   => 1462.7,
    'multiplier'       => 7.312,
    'hours_saved'      => 1367.2,
    'sessions_total'   => 248,
    'sessions_covered' => 11,
);

// Anthropic не публикует точные numerical лимиты Max-подписок, только описательные множители (×5, ×20).
// Цифры ниже — community-observed после удвоения лимитов в мае 2026 (SpaceX deal):
//   Pro    ≈ 88 000 токенов / 5h (было 44k до мая)
//   Max 5× ≈ 440 000–880 000 токенов / 5h
//   Max 20× ≈ 1 700 000–3 500 000 токенов / 5h
// Здесь использованы lower bound значения (более консервативно).
// Sources:
//   https://9to5google.com/2026/05/06/claude-code-is-getting-higher-usage-limits-doubled-for-most-users/
//   https://techcrunch.com/2025/07/28/anthropic-unveils-new-rate-limits-to-curb-claude-code-power-users/
//   https://portkey.ai/blog/claude-code-limits/
// Cache_read tokens НЕ считаются в budget (официально, https://platform.claude.com/docs/en/api/rate-limits) —
// поэтому 162M кэш-токенов показаны отдельно и не попадают в percent.
$budget = array(
    'tokens_used'  => 1201422,
    'limit_5h'     => 440000,
    'limit_20x'    => 1700000,
    'percent_5x'   => round( 1201422 / 440000 * 100 ),
    'percent_20x'  => round( 1201422 / 1700000 * 100 ),
    'cache_tokens' => 161964125,
);

$sentiment = array(
    'profanity_total'  => 47,
    'frustration_avg'  => 0.34,
    'appreciation_avg' => 0.62,
    'top_day'          => array( 'date' => '2026-04-22', 'profanity' => 12 ),
);

// today оценен независимо: «Phase 3 cyberpunk dashboard» — творческая UI-задача,
// ~600 LOC PHP/CSS/JS + i18n + currency + period filter, без ИИ это ~14ч плотной работы.
$today = array(
    'calls'           => 87,
    'cost_usd'        => 0.51,
    'active_hours'    => 1.4,
    'estimated_hours' => 14.0,             // оценка без ИИ
    'hours_saved'     => 12.6,             // 14.0 − 1.4
    'profanity'       => 0,
    'top_session'     => 'Phase 3 cyberpunk dashboard',
);

$models = array(
    array( 'name' => 'opus 4.7',   'calls' => 53854, 'cost' => 54393.94, 'pct' => 90.2 ),
    array( 'name' => 'opus 4.6',   'calls' => 9418,  'cost' => 5043.61,  'pct' => 8.4 ),
    array( 'name' => 'sonnet 4.6', 'calls' => 16444, 'cost' => 861.86,   'pct' => 1.4 ),
);

$sessions = array(
    array( 'phase' => '3',     'desc_ru' => 'Киберпанк-дашборд',          'desc_en' => 'Cyberpunk dashboard impl', 'cost' => 0.51, 'mood' => 'stable' ),
    array( 'phase' => '2',     'desc_ru' => 'Aggregator-бэкенд',           'desc_en' => 'Aggregator backend',        'cost' => 0.44, 'mood' => 'calm' ),
    array( 'phase' => '1.4',   'desc_ru' => 'Sentiment-трекинг',           'desc_en' => 'Sentiment tracking',        'cost' => 0.38, 'mood' => 'stable' ),
    array( 'phase' => '1.0.3', 'desc_ru' => 'Active-time метрика',         'desc_en' => 'Active-time metric',        'cost' => 0.42, 'mood' => 'stable' ),
    array( 'phase' => '1.0.2', 'desc_ru' => 'Hot-fix throttle+pricing',    'desc_en' => 'Hotfix throttle+pricing',   'cost' => 0.62, 'mood' => 'frustrated_calm' ),
    array( 'phase' => '1.0.1', 'desc_ru' => 'Ретроспективный backfill',    'desc_en' => 'Retroactive backfill',      'cost' => 0.49, 'mood' => 'stable' ),
    array( 'phase' => '1.3',   'desc_ru' => 'SessionStart + сложность',    'desc_en' => 'SessionStart + complexity', 'cost' => 0.56, 'mood' => 'calm' ),
    array( 'phase' => '1.0',   'desc_ru' => 'Stop hook + summary',         'desc_en' => 'Stop hook + summary',       'cost' => 0.71, 'mood' => 'stable' ),
);

// 62-day distribution; sum = 79,716 (matches $totals['calls']); peak day = 2547 (index 60).
$timeline_weights = array( 2337, 1193, 1006, 1548, 1482, 177, 131, 1176, 2421, 2136, 1144, 2234, 284, 73, 1020, 1157, 1428, 1459, 2049, 70, 360, 1386, 2506, 2362, 2475, 2136, 282, 174, 1928, 2231, 1557, 969, 1300, 284, 240, 1557, 1291, 1421, 1684, 1175, 106, 261, 1164, 1733, 1700, 2262, 1528, 79, 304, 2117, 1225, 1774, 1126, 2151, 214, 252, 2206, 1368, 2478, 1102, 2547, 176 );
$tl_max = max( $timeline_weights );
$tl_max = $tl_max > 0 ? $tl_max : 1;

// Snapshot URL — backend (port 8089) pisha сюда раз в 15 мин live aggregates;
// если файла нет — страница останется на PHP-baked mock-данных.
$ma_uploads = wp_upload_dir();
$ma_uploads_baseurl = isset( $ma_uploads['baseurl'] ) ? $ma_uploads['baseurl'] : '';
$ma_snapshot_url = wp_make_link_relative(
    trailingslashit( $ma_uploads_baseurl ) . 'multi-agent/snapshot.json'
);
if ( empty( $ma_snapshot_url ) ) {
    $ma_snapshot_url = '/wp-content/uploads/multi-agent/snapshot.json';
}

get_header();
?>

<style>
  .ma-page { max-width: 1280px; margin: 0 auto; padding: 28px 16px 80px; }
  .ma-grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    grid-auto-rows: minmax(110px, auto);
    gap: 10px;
  }
  .ops-panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    position: relative;
    overflow: hidden;
    display: flex; flex-direction: column;
    backdrop-filter: blur(6px);
    transition: all 0.25s;
  }
  .ops-panel:hover { border-color: var(--border-hi); background: var(--panel-hi); }
  .ops-panel > .pheader {
    font-size: 10px; color: var(--text-muted);
    letter-spacing: 0.2em; text-transform: uppercase;
    margin-bottom: 10px;
    display: flex; justify-content: space-between; align-items: center;
    gap: 12px;
    padding-bottom: 6px;
    border-bottom: 1px dashed rgba(0, 212, 255, 0.15);
  }
  .ops-panel > .pheader .ptitle { color: var(--cyan); min-width: 0; }
  .ops-panel > .pheader .pid {
    color: var(--text-muted);
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
    flex-shrink: 0;
    margin-left: auto;
    white-space: nowrap;
  }
  .ops-panel .pbody { flex: 1; font-family: 'JetBrains Mono', monospace; font-size: 12px; }

  .p-today    { grid-column: span 12; grid-row: span 1; }
  .p-combined { grid-column: span 12; grid-row: span 1; }
  .p-savings   { grid-column: span 4; grid-row: span 2; }
  .p-multi     { grid-column: span 4; grid-row: span 2; }
  .p-active    { grid-column: span 4; grid-row: span 2; }
  .p-budget    { grid-column: span 12; grid-row: span 1; }
  .p-sentiment { grid-column: span 4; grid-row: span 2; }
  .p-models    { grid-column: span 4; grid-row: span 2; }
  .p-sessions  { grid-column: span 4; grid-row: span 2; }
  .p-timeline  { grid-column: span 12; grid-row: span 2; }

  .ma-hero {
    position: relative;
    margin-bottom: 14px;
    padding: 18px 22px 16px;
    border-radius: 12px;
    background:
      linear-gradient(135deg, rgba(0, 212, 255, 0.04), rgba(255, 64, 192, 0.03)),
      var(--panel);
    border: 1px solid rgba(0, 212, 255, 0.25);
    backdrop-filter: blur(8px);
    box-shadow: 0 0 30px rgba(0, 212, 255, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.04);
    overflow: hidden;
    animation: maHeroGlow 4s ease-in-out infinite;
  }
  @keyframes maHeroGlow {
    0%, 100% { box-shadow: 0 0 30px rgba(0, 212, 255, 0.12); }
    50%      { box-shadow: 0 0 55px rgba(0, 212, 255, 0.22); }
  }
  .ma-hero-meta {
    display: flex; justify-content: space-between; align-items: center;
    font-family: 'JetBrains Mono', monospace; font-size: 10px;
    color: var(--text-muted); letter-spacing: 0.18em; text-transform: uppercase;
    margin-bottom: 12px;
  }
  .ma-status { display: inline-flex; gap: 8px; align-items: center; color: var(--cyan); }
  .ma-status .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--signal); box-shadow: 0 0 8px var(--signal);
    animation: maPulse 1.6s ease-in-out infinite;
  }
  @keyframes maPulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.55; transform: scale(1.4); } }
  .ma-hero-title { color: var(--text); font-family: 'Manrope', sans-serif; font-weight: 700; font-size: 22px; letter-spacing: -0.01em; margin: 4px 0 8px; }
  .ma-hero-sub { color: var(--text-dim); font-family: 'Inter', sans-serif; font-size: 13px; line-height: 1.55; max-width: 720px; }
  .ma-hero-sub code { background: rgba(0, 212, 255, 0.08); padding: 1px 6px; border-radius: 3px; font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--cyan); }
  .ma-hero-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 14px;
  }
  .ma-kpi-badge {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-height: 30px;
    padding: 6px 9px;
    border: 1px solid rgba(0, 212, 255, 0.22);
    border-radius: 5px;
    background: rgba(0, 212, 255, 0.04);
    font-family: 'JetBrains Mono', monospace;
    white-space: nowrap;
  }
  .ma-kpi-badge .k {
    color: var(--text-muted);
    font-size: 9px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  .ma-kpi-badge .v {
    color: var(--cyan);
    font-size: 11px;
    font-weight: 700;
  }
  .ma-kpi-badge.signal {
    border-color: rgba(106, 255, 211, 0.34);
    background: rgba(106, 255, 211, 0.035);
  }
  .ma-kpi-badge.signal .v { color: var(--signal); }
  .ma-rate-info { font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--text-muted); letter-spacing: 0.1em; margin-top: 10px; padding-top: 8px; border-top: 1px dashed rgba(0, 212, 255, 0.1); }
  .ma-rate-info .v { color: var(--cyan); }
  .ma-data-source {
    display: inline-block; margin-left: 12px;
    padding: 2px 8px;
    border: 1px solid var(--border);
    border-radius: 3px;
    color: var(--text-muted);
    font-size: 9px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }
  .ma-data-source[data-snapshot-status="live"] { border-color: var(--signal); color: var(--signal); }
  .ma-data-source[data-snapshot-status="stale"] { border-color: var(--text-muted); color: var(--text-muted); }
  .ma-data-source[data-snapshot-status="demo"]  { border-color: rgba(255, 122, 89, 0.4); color: #FF7A59; }

  .ma-period-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 0 0 14px;
    padding: 10px 12px;
    border: 1px solid rgba(0, 212, 255, 0.18);
    border-radius: 8px;
    background: rgba(0, 212, 255, 0.03);
    font-family: 'JetBrains Mono', monospace;
  }
  .ma-period-label {
    color: var(--text-muted);
    font-size: 10px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin-right: 4px;
  }
  .ma-period-btn {
    appearance: none;
    border: 1px solid rgba(0, 212, 255, 0.35);
    border-radius: 4px;
    background: rgba(0, 212, 255, 0.04);
    color: var(--text-dim);
    cursor: pointer;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.08em;
    line-height: 1;
    padding: 8px 10px;
    text-transform: uppercase;
    transition: border-color 0.16s, color 0.16s, background 0.16s, box-shadow 0.16s;
  }
  .ma-period-btn:hover {
    border-color: var(--cyan);
    color: var(--cyan);
    box-shadow: 0 0 12px rgba(0, 212, 255, 0.14);
  }
  .ma-period-btn.ma-active {
    background: var(--cyan);
    border-color: var(--cyan);
    color: var(--bg);
    font-weight: 700;
    box-shadow: 0 0 20px rgba(0, 212, 255, 0.55);
  }
  .ma-methodology {
    margin: 0 0 14px;
    border: 1px solid rgba(0, 212, 255, 0.18);
    border-radius: 8px;
    background: rgba(0, 212, 255, 0.025);
    color: var(--text-dim);
    font-family: 'Inter', sans-serif;
  }
  .ma-methodology summary {
    cursor: pointer;
    color: var(--cyan);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.14em;
    list-style: none;
    padding: 12px 14px;
    text-transform: uppercase;
  }
  .ma-methodology summary::-webkit-details-marker { display: none; }
  .ma-methodology summary::before {
    content: '+';
    display: inline-block;
    margin-right: 10px;
    color: var(--signal);
  }
  .ma-methodology[open] summary::before { content: '−'; }
  .ma-method-body {
    border-top: 1px dashed rgba(0, 212, 255, 0.14);
    padding: 14px;
  }
  .ma-method-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 10px;
  }
  .ma-method-card {
    min-width: 0;
    border: 1px solid rgba(0, 212, 255, 0.1);
    border-radius: 5px;
    background: rgba(0, 212, 255, 0.025);
    padding: 10px;
  }
  .ma-method-card .k {
    color: var(--text-muted);
    display: block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.12em;
    margin-bottom: 6px;
    text-transform: uppercase;
  }
  .ma-method-card .v {
    color: var(--text-dim);
    font-size: 12px;
    line-height: 1.45;
  }
  .ma-method-card code {
    color: var(--cyan);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
  }

  .big-number {
    font-family: 'Manrope', sans-serif; font-weight: 800;
    font-size: 38px; color: var(--cyan); line-height: 1;
    text-shadow: var(--glow);
    margin: 8px 0 4px;
  }
  .big-number.signal { color: var(--signal); text-shadow: var(--s-glow); }
  .big-number-sub {
    color: var(--text-muted); font-size: 11px; letter-spacing: 0.12em;
    text-transform: uppercase; margin-top: 6px;
  }
  .big-number-detail {
    color: var(--text-dim); font-family: 'JetBrains Mono', monospace;
    font-size: 11px; margin-top: 8px; line-height: 1.5;
  }
  .big-number-detail .v { color: var(--cyan); }

  .combined-body {
    display: grid;
    grid-template-columns: minmax(220px, 0.9fr) minmax(320px, 1.3fr);
    gap: 18px;
    align-items: center;
  }
  .combined-provider-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .prov-row {
    display: grid;
    grid-template-columns: minmax(150px, 1fr) 110px 120px;
    gap: 12px;
    align-items: center;
    border: 1px solid rgba(0, 212, 255, 0.12);
    border-radius: 5px;
    background: rgba(0, 212, 255, 0.03);
    padding: 9px 10px;
  }
  .prov-name {
    color: var(--text);
    font-family: 'Inter', sans-serif;
    font-size: 12px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .prov-calls,
  .prov-cost {
    color: var(--cyan);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 600;
    text-align: right;
    white-space: nowrap;
  }
  .combined-foot {
    color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.08em;
    line-height: 1.5;
    margin-top: 8px;
    text-transform: uppercase;
  }
  .combined-foot .v { color: var(--cyan); }

  .finance-sub {
    color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.1em;
    line-height: 1.45;
    margin-top: 8px;
    text-transform: uppercase;
  }
  .finance-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 18px;
  }
  .finance-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: baseline;
    padding-bottom: 9px;
    border-bottom: 1px dashed rgba(0, 212, 255, 0.14);
  }
  .finance-row:last-child { border-bottom: 0; padding-bottom: 0; }
  .finance-row .k {
    color: var(--text-muted);
    font-size: 11px;
    letter-spacing: 0.08em;
    line-height: 1.35;
    text-transform: uppercase;
  }
  .finance-row .v {
    color: var(--cyan);
    font-size: 12px;
    font-weight: 600;
    white-space: nowrap;
  }
  .finance-row.primary .v { color: var(--signal); }
  .finance-note {
    border-top: 1px dashed rgba(0, 212, 255, 0.14);
    color: var(--text-dim);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    line-height: 1.5;
    margin-top: 16px;
    padding-top: 12px;
  }
  .finance-note .v {
    color: var(--cyan);
    font-weight: 700;
  }

  .budget-row { display: flex; flex-direction: column; gap: 8px; }
  .budget-meta {
    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
    align-items: center;
    gap: 12px; font-family: 'JetBrains Mono', monospace; font-size: 11px;
  }
  .budget-meta > span { min-width: 0; }
  .budget-meta .k { color: var(--text-muted); letter-spacing: 0.12em; text-transform: uppercase; }
  .budget-meta .v { color: var(--cyan); font-weight: 600; }
  .budget-meta .v.hot { color: var(--hot); }
  .budget-bar { height: 14px; background: rgba(0, 212, 255, 0.05); border-radius: 3px; overflow: hidden; border: 1px solid rgba(0, 212, 255, 0.1); position: relative; }
  .budget-fill {
    height: 100%; background: linear-gradient(90deg, var(--cyan), var(--signal));
    box-shadow: 0 0 8px rgba(0, 212, 255, 0.4);
  }
  .budget-cap-marker { position: absolute; top: -2px; bottom: -2px; width: 2px; background: var(--text-muted); }

  .today-metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
  }
  .today-metric {
    min-width: 0;
    border: 1px solid rgba(0, 212, 255, 0.12);
    border-radius: 5px;
    background: rgba(0, 212, 255, 0.035);
    padding: 10px 12px;
  }
  .today-metric .k {
    color: var(--text-muted);
    display: block;
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  .today-metric .v {
    color: var(--cyan);
    display: block;
    font-family: 'Manrope', sans-serif;
    font-size: 22px;
    font-weight: 800;
    line-height: 1.1;
    margin-top: 6px;
    text-shadow: var(--glow);
  }
  .today-session {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px dashed rgba(0, 212, 255, 0.12);
    color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .today-session .v { color: var(--text-dim); margin-left: 8px; }

  .sent-list { display: flex; flex-direction: column; gap: 10px; margin-top: 4px; }
  .sent-row { display: grid; grid-template-columns: 110px 1fr 40px; gap: 10px; align-items: center; font-size: 11px; }
  .sent-label { color: var(--text-dim); letter-spacing: 0.1em; text-transform: uppercase; font-size: 10px; font-family: 'JetBrains Mono', monospace; }
  .sent-track { background: rgba(0, 212, 255, 0.05); border-radius: 2px; height: 10px; overflow: hidden; border: 1px solid rgba(0, 212, 255, 0.1); }
  .sent-fill { display: block; height: 100%; box-shadow: 0 0 8px rgba(0, 212, 255, 0.4); }
  .sent-fill.signal { background: linear-gradient(90deg, var(--signal), var(--cyan)); }
  .sent-fill.warn   { background: linear-gradient(90deg, var(--warning), var(--hot)); }
  .sent-val { color: var(--cyan); text-align: right; font-family: 'JetBrains Mono', monospace; font-weight: 600; }
  .sent-extra { margin-top: 12px; padding-top: 10px; border-top: 1px dashed rgba(0, 212, 255, 0.12); font-size: 11px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; line-height: 1.5; }
  .sent-extra .v { color: var(--cyan); }

  .bar-list { display: flex; flex-direction: column; gap: 8px; }
  .bar-row { display: grid; grid-template-columns: 100px 1fr 80px; gap: 10px; align-items: center; font-size: 11px; }
  .bar-label { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'JetBrains Mono', monospace; }
  .bar-track { background: rgba(0, 212, 255, 0.05); border-radius: 2px; height: 14px; overflow: hidden; border: 1px solid rgba(0, 212, 255, 0.1); }
  .bar-fill { display: block; height: 100%; background: linear-gradient(90deg, var(--cyan), var(--signal)); box-shadow: 0 0 8px rgba(0, 212, 255, 0.4); }
  .bar-cnt { color: var(--cyan); text-align: right; font-family: 'JetBrains Mono', monospace; font-weight: 600; font-size: 11px; }

  .sess-list { display: flex; flex-direction: column; gap: 2px; font-size: 11px; }
  .sess-row {
    display: grid; grid-template-columns: 60px 1fr auto; gap: 10px;
    padding: 5px 8px; align-items: center;
    border-bottom: 1px solid rgba(0, 212, 255, 0.08);
    transition: all 0.15s;
  }
  .sess-row:hover { background: rgba(0, 212, 255, 0.04); padding-left: 14px; }
  .sess-row .sp { color: var(--cyan); font-size: 10px; padding: 2px 8px; border: 1px solid var(--border); border-radius: 10px; text-align: center; letter-spacing: 0.08em; font-family: 'JetBrains Mono', monospace; }
  .sess-row .sd { color: var(--text); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'Inter', sans-serif; }
  .sess-row .sc { color: var(--text-dim); font-family: 'JetBrains Mono', monospace; font-size: 10px; }
  .sess-row .mood { color: var(--text-muted); font-size: 9px; margin-left: 6px; }

  .ma-timeline { display: flex; align-items: flex-end; gap: 3px; height: 130px; padding-bottom: 18px; position: relative; }
  .ma-tl-bar { flex: 1; position: relative; display: flex; flex-direction: column; justify-content: flex-end; height: 100%; cursor: crosshair; }
  .ma-tl-fill { background: linear-gradient(180deg, var(--cyan), rgba(0, 212, 255, 0.3)); border-radius: 1px 1px 0 0; box-shadow: 0 0 6px rgba(0, 212, 255, 0.3); min-height: 1px; }
  .ma-tl-bar:hover .ma-tl-fill { background: linear-gradient(180deg, var(--signal), var(--cyan)); box-shadow: 0 0 12px rgba(106, 255, 211, 0.55); }
  .ma-tl-tooltip {
    position: fixed; z-index: 9999; pointer-events: none; opacity: 0; transform: translateY(4px);
    min-width: 220px; padding: 10px 12px; border: 1px solid rgba(0, 212, 255, 0.35);
    background: rgba(5, 13, 26, 0.96); color: var(--text); box-shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
    font-family: 'JetBrains Mono', monospace; font-size: 11px; line-height: 1.45; transition: opacity 0.12s, transform 0.12s;
  }
  .ma-tl-tooltip.visible { opacity: 1; transform: translateY(0); }
  .ma-tl-tooltip .tt-date { color: var(--cyan); font-weight: 700; letter-spacing: 0.08em; margin-bottom: 6px; }
  .ma-tl-tooltip .tt-row { display: flex; justify-content: space-between; gap: 18px; border-top: 1px dashed rgba(0, 212, 255, 0.12); padding-top: 4px; margin-top: 4px; }
  .ma-tl-tooltip .tt-k { color: var(--text-muted); }
  .ma-tl-tooltip .tt-v { color: var(--text); text-align: right; }
  .ma-tl-tooltip .tt-muted { color: var(--text-muted); }
  .ma-tl-summary { margin-top: 8px; display: flex; gap: 12px; align-items: center; font-family: 'JetBrains Mono', monospace; font-size: 10px; }
  .ma-tl-summary .k { color: var(--text-muted); letter-spacing: 0.15em; text-transform: uppercase; }
  .ma-tl-summary .v { color: var(--cyan); font-weight: 600; }
  .ma-tl-summary .sep { color: var(--text-muted); }

  @media (max-width: 1100px) {
    .ops-panel { grid-column: span 6 !important; grid-row: auto !important; }
    .p-today, .p-combined, .p-budget, .p-timeline { grid-column: span 12 !important; }
    .combined-body { grid-template-columns: 1fr; }
    .ma-method-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 640px) {
    .ma-grid { grid-template-columns: 1fr; gap: 8px; }
    .ops-panel { grid-column: span 1 !important; }
    .big-number { font-size: 32px; }
    .today-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .ma-method-grid { grid-template-columns: 1fr; }
    .prov-row { grid-template-columns: 1fr; gap: 4px; }
    .prov-calls, .prov-cost { text-align: left; }
  }
</style>

<main class="ma-page">

  <section class="ma-hero">
    <div class="ma-hero-meta">
      <span class="ma-status"><span class="dot"></span><span data-ma-i18n="status">ТРЕКЕР · LIVE SNAPSHOT · ЛОКАЛЬНЫЕ ДАННЫЕ</span></span>
      <span data-ma-i18n="meta">4 ИСТОЧНИКА · 3 ОСИ ОЦЕНКИ · СНИМОК ДЛЯ ПУБЛИКАЦИИ</span>
    </div>
    <div class="ma-hero-title">Multi-Agent Tracker</div>
    <div class="ma-hero-sub" data-ma-i18n-html="hero_sub">
      ОПЕРАЦИОННЫЙ ЖУРНАЛ. После каждой сессии остаются три цифры:
      <code>деньги</code> — цена API-токенов, если бы не подписка,
      <code>часы</code> — ручной маршрут без ИИ,
      <code>стресс</code> — мат и закипание в стенограммах. Живой поток шуршит локально на 8089.
      На экране — только снимок, чтобы видеть масштаб.
    </div>
    <div class="ma-hero-badges" aria-label="Snapshot highlights">
      <span class="ma-kpi-badge">
        <span class="k" data-ma-i18n="hero_badge_sources">источники</span>
        <span class="v"><span data-hero-provider-count><?php echo count( $providers ); ?></span></span>
      </span>
      <span class="ma-kpi-badge">
        <span class="k" data-ma-i18n="hero_badge_sessions">сессии оценены</span>
        <span class="v"><span data-hero-sessions-covered><?php echo (int) $productivity['sessions_covered']; ?></span>/<span data-hero-sessions-total><?php echo (int) $productivity['sessions_total']; ?></span></span>
      </span>
      <span class="ma-kpi-badge signal">
        <span class="k" data-ma-i18n="hero_badge_multiplier">множитель</span>
        <span class="v">×<span data-hero-multiplier><?php echo number_format( $productivity['multiplier'], 1 ); ?></span></span>
      </span>
      <span class="ma-kpi-badge signal">
        <span class="k" data-ma-i18n="hero_badge_savings">чистая экономия</span>
        <span class="v" data-usd="<?php echo esc_attr( $totals['savings_usd'] ); ?>" data-hero-savings></span>
      </span>
      <span class="ma-kpi-badge">
        <span class="k" data-ma-i18n="hero_badge_privacy">публикация</span>
        <span class="v" data-ma-i18n="hero_badge_privacy_value">public-safe</span>
      </span>
    </div>
    <div class="ma-rate-info">
      <span data-ma-i18n="rate_label">Курс ЦБ РФ:</span>
      <span class="v">1 USD = <?php echo number_format( $rate, 2, '.', ' ' ); ?> ₽</span>
      <span data-ma-i18n="rate_note">(используется для конвертации в рублях; обновляется раз в 12 часов)</span>
      <span class="ma-data-source" data-snapshot-status="loading"><span data-ma-i18n="ds_loading">проверяю снимок…</span></span>
    </div>
  </section>

  <div class="ma-period-bar" role="group" aria-label="Period selector">
    <span class="ma-period-label" data-ma-i18n="period_label">ПЕРИОД</span>
    <button class="ma-period-btn" type="button" data-period="today" data-ma-i18n="period_today" aria-pressed="false">сегодня</button>
    <button class="ma-period-btn" type="button" data-period="7d" data-ma-i18n="period_7d" aria-pressed="false">7 дней</button>
    <button class="ma-period-btn" type="button" data-period="30d" data-ma-i18n="period_30d" aria-pressed="false">30 дней</button>
    <button class="ma-period-btn" type="button" data-period="60d" data-ma-i18n="period_60d" aria-pressed="false">60 дней</button>
    <button class="ma-period-btn ma-active" type="button" data-period="all" data-ma-i18n="period_all" aria-pressed="true">всё время</button>
  </div>

  <details class="ma-methodology">
    <summary data-ma-i18n="method_summary">методология и приватность</summary>
    <div class="ma-method-body">
      <div class="ma-method-grid">
        <div class="ma-method-card">
          <span class="k" data-ma-i18n="method_sources_title">источники</span>
          <span class="v" data-ma-i18n-html="method_sources_body">Claude Code hook, Codex local sessions/wrapper, OpenClaw JSONL и OpenCode SQLite. В snapshot попадает только учётная метаинформация: модель, токены, стоимость, время и агрегаты.</span>
        </div>
        <div class="ma-method-card">
          <span class="k" data-ma-i18n="method_money_title">деньги</span>
          <span class="v" data-ma-i18n-html="method_money_body">API-эквивалент считается по фактической стоимости провайдера или тарифам API. Подписки делятся на 30 дней. Чистая экономия = <code>API-эквивалент − подписки</code>.</span>
        </div>
        <div class="ma-method-card">
          <span class="k" data-ma-i18n="method_time_title">время</span>
          <span class="v" data-ma-i18n-html="method_time_body">Активное время — промежутки между событиями до 2 минут. «Без ИИ» — оценка трудоёмкости сессии через отдельный estimator плюс ручные правки. Codex/OpenClaw/OpenCode не удваивают productivity.</span>
        </div>
        <div class="ma-method-card">
          <span class="k" data-ma-i18n="method_stress_title">стресс</span>
          <span class="v" data-ma-i18n-html="method_stress_body">Мат считается только в пользовательских сообщениях. Недовольство и благодарность — агрегированная оценка сессий; тексты сообщений на страницу не выводятся.</span>
        </div>
        <div class="ma-method-card">
          <span class="k" data-ma-i18n="method_privacy_title">публикация</span>
          <span class="v" data-ma-i18n-html="method_privacy_body">Публичный режим хеширует session id, вырезает пути, email, токены и слова из blocklist. Перед публикацией snapshot дополнительно проверяется на персональные строки.</span>
        </div>
      </div>
    </div>
  </details>

  <div class="ma-grid">

    <div class="ops-panel p-today">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_today">СЕГОДНЯ</span><span class="pid">00 · <span data-ma-i18n="status_live">LIVE</span></span></div>
      <div class="pbody">
        <div class="today-metrics">
          <div class="today-metric">
            <span class="k" data-ma-i18n="today_calls">вызовов</span>
            <span class="v"><?php echo number_format( $today['calls'], 0, '.', ' ' ); ?></span>
          </div>
          <div class="today-metric">
            <span class="k" data-ma-i18n="today_cost">API-эквив.</span>
            <span class="v" data-usd="<?php echo esc_attr( $today['cost_usd'] ); ?>" data-decimals="2"></span>
          </div>
          <div class="today-metric">
            <span class="k" data-ma-i18n="today_active">активно</span>
            <span class="v"><?php echo number_format( $today['active_hours'], 1 ); ?><span data-ma-i18n="unit_hours_short"> ч</span></span>
          </div>
          <div class="today-metric">
            <span class="k" data-ma-i18n="today_profanity">недовольство</span>
            <span class="v"><?php echo (int) $today['profanity']; ?></span>
          </div>
        </div>
        <div class="today-session">
          <span class="k" data-ma-i18n="today_top_session">главная сессия</span>
          <span class="v"><?php echo esc_html( $today['top_session'] ); ?></span>
        </div>
      </div>
    </div>

    <div class="ops-panel p-combined">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_combined">ЧИСТАЯ ЭКОНОМИЯ</span><span class="pid">09</span></div>
      <div class="pbody combined-body">
        <div>
          <div class="big-number signal"
               data-base-usd="<?php echo esc_attr( $combined['savings_usd'] ); ?>"></div>
          <div class="big-number-sub">
            <span data-ma-i18n="combined_sub">API-эквивалент минус подписки за</span>
            <span data-period-days><?php echo (int) $totals['days']; ?></span>
            <span data-period-days-noun>дней</span>
          </div>
          <div class="combined-foot">
            <span data-ma-i18n="combined_api_eq">API-эквивалент:</span>
            <span class="v" data-base-usd="<?php echo esc_attr( $combined['cost_usd'] ); ?>" data-combined-api></span>
            <span class="sep">·</span>
            <span data-ma-i18n="combined_subscriptions">подписки:</span>
            <span class="v" data-base-usd="<?php echo esc_attr( $combined['subscription_usd'] ); ?>" data-combined-subscription></span>
          </div>
        </div>
        <div class="combined-provider-list">
          <div class="prov-row" data-snap-provider="anthropic_claude">
            <span class="prov-name">Claude (Anthropic)</span>
            <span><span class="prov-calls" data-base-int="<?php echo esc_attr( $providers['anthropic_claude']['calls'] ); ?>"><?php echo number_format( $providers['anthropic_claude']['calls'], 0, '.', ' ' ); ?></span> <span data-ma-i18n="calls_short">вызовов</span></span>
            <span class="prov-cost" data-base-usd="<?php echo esc_attr( $providers['anthropic_claude']['cost_usd'] ); ?>"></span>
          </div>
          <div class="prov-row" data-snap-provider="openai_codex">
            <span class="prov-name">Codex (OpenAI)</span>
            <span><span class="prov-calls" data-base-int="<?php echo esc_attr( $providers['openai_codex']['calls'] ); ?>"><?php echo number_format( $providers['openai_codex']['calls'], 0, '.', ' ' ); ?></span> <span data-ma-i18n="calls_short">вызовов</span></span>
            <span class="prov-cost" data-base-usd="<?php echo esc_attr( $providers['openai_codex']['cost_usd'] ); ?>"></span>
          </div>
          <div class="prov-row" data-snap-provider="openrouter_openclaw">
            <span class="prov-name">OpenClaw (OpenRouter)</span>
            <span><span class="prov-calls" data-base-int="<?php echo esc_attr( $providers['openrouter_openclaw']['calls'] ); ?>"><?php echo number_format( $providers['openrouter_openclaw']['calls'], 0, '.', ' ' ); ?></span> <span data-ma-i18n="calls_short">вызовов</span></span>
            <span class="prov-cost" data-base-usd="<?php echo esc_attr( $providers['openrouter_openclaw']['cost_usd'] ); ?>"></span>
          </div>
          <div class="prov-row" data-snap-provider="opencode_openrouter">
            <span class="prov-name">OpenCode (OpenRouter)</span>
            <span><span class="prov-calls" data-base-int="<?php echo esc_attr( $providers['opencode_openrouter']['calls'] ); ?>"><?php echo number_format( $providers['opencode_openrouter']['calls'], 0, '.', ' ' ); ?></span> <span data-ma-i18n="calls_short">вызовов</span></span>
            <span class="prov-cost" data-base-usd="<?php echo esc_attr( $providers['opencode_openrouter']['cost_usd'] ); ?>"></span>
          </div>
        </div>
      </div>
    </div>

    <div class="ops-panel p-savings">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_savings">ФИНАНСОВАЯ ФОРМУЛА</span><span class="pid">01</span></div>
      <div class="pbody">
        <div class="finance-sub">
          <span data-ma-i18n="savings_formula_sub">расшифровка главного KPI за</span>
          <span data-period-days><?php echo (int) $totals['days']; ?></span>
          <span data-period-days-noun>дней</span>
        </div>
        <div class="finance-list">
          <div class="finance-row">
            <span class="k" data-ma-i18n="api_eq">Эквивалент по API:</span>
            <span class="v"
                  data-base-usd="<?php echo esc_attr( $totals['cost_usd'] ); ?>"
                  data-today-usd="<?php echo esc_attr( $today['cost_usd'] ); ?>"
                  data-ratio-input="api"></span>
          </div>
          <div class="finance-row">
            <span class="k" data-ma-i18n="subscription">Подписка:</span>
            <span class="v"
                  data-base-usd="<?php echo esc_attr( $totals['subscription_usd'] ); ?>"
                  data-ratio-input="sub"></span>
          </div>
          <div class="finance-row primary">
            <span class="k" data-ma-i18n="net_savings">Чистая экономия:</span>
            <span class="v"
                  data-base-usd="<?php echo esc_attr( $totals['savings_usd'] ); ?>"
                  data-today-usd="-6.16"
                  data-savings-net></span>
          </div>
        </div>
        <div class="finance-note">
          <span data-ma-i18n="ratio">Окупаемость:</span>
          <span class="v" data-ratio-display>×<?php echo round( $totals['cost_usd'] / max( $totals['subscription_usd'], 1 ) ); ?></span>
        </div>
      </div>
    </div>

    <div class="ops-panel p-multi">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_multi">МНОЖИТЕЛЬ ПРОИЗВОДИТЕЛЬНОСТИ</span><span class="pid">02</span></div>
      <div class="pbody">
        <div class="big-number signal">×<span data-multi-display><?php echo number_format( $productivity['multiplier'], 1 ); ?></span></div>
        <div class="big-number-sub" data-ma-i18n="multi_sub">чистое время с ИИ против ручной оценки</div>
        <div class="big-number-detail">
          <?php
          // Snapshot v2 overrides this panel from productivity_periods so the
          // multiplier reflects the selected period instead of a linear
          // pro-rate of the all-time ratio.
          ?>
          <span data-ma-i18n="with_ai">С ИИ:</span> <span class="v"
                data-base-hours="<?php echo esc_attr( $productivity['active_hours'] ); ?>"
                data-multi-input="with-ai"
                data-snap-with-ai
                data-decimals="1"></span><span data-ma-i18n="unit_hours_short"> ч</span><br>
          <span data-ma-i18n="without_ai">Без ИИ (оценка):</span> <span class="v"
                data-base-hours="<?php echo esc_attr( $productivity['active_hours'] * $productivity['multiplier'] ); ?>"
                data-snap-without-ai
                data-decimals="1"></span><span data-ma-i18n="unit_hours_short"> ч</span><br>
          <span data-ma-i18n="hours_saved">Сэкономлено часов:</span> <span class="v"
                data-base-hours="<?php echo esc_attr( $productivity['hours_saved'] ); ?>"
                data-multi-input="saved"
                data-snap-hours-saved
                data-decimals="1"></span><span data-ma-i18n="unit_hours_short"> ч</span>
        </div>
      </div>
    </div>

    <div class="ops-panel p-active">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_active">АКТИВНОЕ ВРЕМЯ</span><span class="pid">03</span></div>
      <div class="pbody">
        <div class="big-number"><span class="ma-hours-big"
                data-base-hours="<?php echo esc_attr( $productivity['active_hours'] ); ?>"
                data-today-hours="<?php echo esc_attr( $today['active_hours'] ); ?>"
                data-snap-active-hours
                data-decimals="1"><?php echo number_format( $productivity['active_hours'], 1 ); ?></span><span data-ma-i18n="unit_hours_short"> ч</span></div>
        <div class="big-number-sub" data-ma-i18n="active_sub">считаются паузы между сообщениями короче 2 минут</div>
        <div class="big-number-detail">
          <span data-ma-i18n="cal_span">Календарный диапазон:</span> <span class="v"
                data-base-hours="<?php echo esc_attr( $productivity['calendar_hours'] ); ?>"
                data-today-hours="24"
                data-snap-calendar-hours
                data-decimals="0"></span><span data-ma-i18n="unit_hours_short"> ч</span><br>
          <span data-ma-i18n="sess_covered">Сессий оценено:</span>
          <span class="v">
            <span data-snap-productivity="sessions_covered"><?php echo (int) $productivity['sessions_covered']; ?></span> /
            <span data-snap-productivity="sessions_total"><?php echo (int) $productivity['sessions_total']; ?></span>
          </span><br>
          <span data-ma-i18n="avg_day">В среднем в день:</span> <span class="v ma-avgday"
                data-avg-display
                data-decimals="1"><?php echo number_format( $productivity['active_hours'] / $totals['days'], 1 ); ?></span><span data-ma-i18n="unit_hours_short"> ч</span>
        </div>
      </div>
    </div>

    <div class="ops-panel p-budget">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_budget">5-ЧАСОВОЙ БЮДЖЕТ · НОРМА</span><span class="pid">04 · <span data-ma-i18n="status_live">LIVE</span></span></div>
      <div class="pbody">
        <div class="budget-row">
          <div class="budget-meta">
            <span><span class="k" data-ma-i18n="tokens_used">токенов использовано</span> <span class="v" data-snap-budget="tokens_used"><?php echo number_format( $budget['tokens_used'], 0, '.', ' ' ); ?></span></span>
            <span><span class="k" data-ma-i18n="limit_max5x">лимит Max 5×</span> <span class="v" data-snap-budget="limit_5h"><?php echo number_format( $budget['limit_5h'], 0, '.', ' ' ); ?></span></span>
            <span><span class="k" data-ma-i18n="percent">% от Max 5×</span> <span class="v" data-snap-budget-pct="percent_5x"><?php echo (int) $budget['percent_5x']; ?>%</span></span>
          </div>
          <div class="budget-bar">
            <div class="budget-fill" style="width: <?php echo (int) min( 100, $budget['percent_20x'] ); ?>%;"></div>
            <div class="budget-cap-marker" style="left: 25.9%;" title="Max 5× ≈ 440k" data-ma-i18n-title="cap_max5x_title"></div>
          </div>
          <div class="budget-meta" style="font-size: 10px;">
            <span><span class="k" data-ma-i18n="cache_tokens">кэш-токены вне лимита</span> <span class="v" data-snap-budget="cache_tokens"><?php echo number_format( $budget['cache_tokens'], 0, '.', ' ' ); ?></span></span>
            <span><span class="k" data-ma-i18n="vs_max20">% от Max 20×</span> <span class="v" data-snap-budget-pct="percent_20x"><?php echo (int) $budget['percent_20x']; ?>%</span></span>
            <span><span class="k" data-ma-i18n="budget_status">статус</span> <span class="v" data-ma-i18n="mode_normal">НОРМА</span></span>
          </div>
        </div>
      </div>
    </div>

    <div class="ops-panel p-sentiment">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_sentiment">ТОНАЛЬНОСТЬ · УРОВЕНЬ СТРЕССА</span><span class="pid">05</span></div>
      <div class="pbody">
        <div class="sent-list">
          <div class="sent-row" data-snap-sent-row="frustration_avg">
            <span class="sent-label" data-ma-i18n="frustration">недовольство</span>
            <span class="sent-track"><span class="sent-fill warn" style="width: <?php echo round( $sentiment['frustration_avg'] * 100 ); ?>%;"></span></span>
            <span class="sent-val"><?php echo number_format( $sentiment['frustration_avg'], 2 ); ?></span>
          </div>
          <div class="sent-row" data-snap-sent-row="appreciation_avg">
            <span class="sent-label" data-ma-i18n="appreciation">благодарность</span>
            <span class="sent-track"><span class="sent-fill signal" style="width: <?php echo round( $sentiment['appreciation_avg'] * 100 ); ?>%;"></span></span>
            <span class="sent-val"><?php echo number_format( $sentiment['appreciation_avg'], 2 ); ?></span>
          </div>
        </div>
        <div class="sent-extra">
          <span data-ma-i18n="prof_total">матов всего:</span> <span class="v" data-snap-sent="profanity_total"><?php echo (int) $sentiment['profanity_total']; ?></span><br>
          <span data-ma-i18n="top_day">пиковый день:</span> <span class="v" data-snap-sent-topday-date><?php echo esc_html( $sentiment['top_day']['date'] ); ?></span> (<span data-snap-sent-topday-prof><?php echo (int) $sentiment['top_day']['profanity']; ?></span> <span data-ma-i18n="prof_units">матов</span>)<br>
          <span data-ma-i18n="stress_trend">тренд стресса:</span> <span class="v" data-snap-sent-stress-trend>↘ <span data-ma-i18n="trend_improving">улучшение</span></span>
        </div>
      </div>
    </div>

    <div class="ops-panel p-models">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_models">МОДЕЛИ · РАСПРЕДЕЛЕНИЕ</span><span class="pid">06</span></div>
      <div class="pbody">
        <div class="bar-list" data-snap-models-list>
          <?php foreach ( $models as $m ) : ?>
          <div class="bar-row">
            <span class="bar-label"><?php echo esc_html( $m['name'] ); ?></span>
            <span class="bar-track"><span class="bar-fill" style="width: <?php echo esc_attr( $m['pct'] ); ?>%;"></span></span>
            <span class="bar-cnt" data-usd="<?php echo esc_attr( $m['cost'] ); ?>"></span>
          </div>
          <?php endforeach; ?>
        </div>
      </div>
    </div>

    <div class="ops-panel p-sessions">
      <div class="pheader"><span class="ptitle" data-ma-i18n="p_sessions">ПОСЛЕДНИЕ СЕССИИ</span><span class="pid">07</span></div>
      <div class="pbody">
        <div class="sess-list" data-snap-sessions-list>
          <?php foreach ( $sessions as $s ) : ?>
          <div class="sess-row">
            <span class="sp"><span data-ma-i18n="phase_short">P</span><?php echo esc_html( $s['phase'] ); ?></span>
            <span class="sd"><span data-ma-i18n-text="<?php echo esc_attr( $s['desc_en'] ); ?>"><?php echo esc_html( $s['desc_ru'] ); ?></span><span class="mood" data-ma-i18n="mood_<?php echo esc_attr( str_replace( array( '→', ' ' ), array( '_', '_' ), $s['mood'] ) ); ?>"><?php echo esc_html( $s['mood'] ); ?></span></span>
            <span class="sc" data-usd="<?php echo esc_attr( $s['cost'] ); ?>" data-decimals="2"></span>
          </div>
          <?php endforeach; ?>
        </div>
      </div>
    </div>

    <div class="ops-panel p-timeline">
      <div class="pheader"><span class="ptitle"><span data-ma-i18n="p_timeline_prefix">ХРОНОЛОГИЯ АКТИВНОСТИ</span> · <span data-period-days><?php echo (int) $totals['days']; ?></span> <span data-period-days-noun-up>ДНЕЙ</span></span><span class="pid">08</span></div>
      <div class="pbody">
        <div class="ma-timeline">
          <?php foreach ( $timeline_weights as $idx => $w ) :
              $h = ( $w / $tl_max ) * 100;
          ?>
          <div class="ma-tl-bar" data-day-idx="<?php echo (int) $idx; ?>" data-day-calls="<?php echo (int) $w; ?>" title="<?php echo (int) $w; ?>">
            <div class="ma-tl-fill" style="height: <?php echo esc_attr( $h ); ?>%;"></div>
          </div>
          <?php endforeach; ?>
        </div>
        <div class="ma-tl-summary">
          <span class="k" data-ma-i18n="tl_period">период</span>
          <span class="v" data-snap-tl-range><?php echo esc_html( $totals['period_start'] ); ?> → <?php echo esc_html( $totals['period_end'] ); ?></span>
          <span class="sep">·</span>
          <span class="k" data-ma-i18n="tl_total">всего событий</span>
          <span class="v"
                data-snap-total-calls
                data-base-int="<?php echo esc_attr( $totals['calls'] ); ?>"
                data-today-int="<?php echo esc_attr( $today['calls'] ); ?>"><?php echo number_format( $totals['calls'], 0, '.', ' ' ); ?></span>
          <span class="sep">·</span>
          <span class="k" data-ma-i18n="tl_peak">пиковый день</span>
          <span class="v"><span data-snap-tl-peak-calls>2547</span> <span data-ma-i18n="calls_unit">calls</span> (<span data-snap-tl-peak-date>2026-05-09</span>)</span>
        </div>
      </div>
    </div>

  </div>
</main>

<script>
(function() {
  // === Multi-Agent Tracker · i18n + currency conversion ===
  // Default lang = ru. ?lang=en переключает. Курс ЦБ РФ embedded из PHP.
  const RATE = <?php echo wp_json_encode( $rate ); ?>;
  const SNAPSHOT_URL = <?php echo wp_json_encode( $ma_snapshot_url ); ?>;

  const TRANSLATIONS = {
    ru: {
      status: 'ТРЕКЕР · ЖИВОЙ СНИМОК · ЛОКАЛЬНЫЕ ДАННЫЕ',
      status_live: 'ЛАЙВ',
      unit_hours_short: ' ч',
      mode_burst: 'ПЕРЕРАСХОД',
      mode_normal: 'НОРМА',
      phase_short: 'Ф',
      calls_unit: 'событий',
      calls_short: 'вызовов',
      cap_max5x_title: 'лимит подписки Max 5×',
      cap_max20x_title: 'лимит подписки Max 20×',
      mood_stable: 'стабильно',
      mood_calm: 'спокойно',
      mood_frustrated_calm: 'недовольство → спокойствие',
      meta: '4 ИСТОЧНИКА · 3 ОСИ ОЦЕНКИ · СНИМОК ДЛЯ ПУБЛИКАЦИИ',
      hero_sub: 'ОПЕРАЦИОННЫЙ ЖУРНАЛ. После каждой сессии остаются три цифры: <code>деньги</code> — API-эквивалент и стоимость подписок, <code>часы</code> — активное время против ручной оценки, <code>стресс</code> — агрегированный эмоциональный след. На странице только обезличенный snapshot без текстов переписок.',
      hero_badge_sources: 'источники',
      hero_badge_sessions: 'сессии оценены',
      hero_badge_multiplier: 'множитель',
      hero_badge_savings: 'чистая экономия',
      hero_badge_privacy: 'публикация',
      hero_badge_privacy_value: 'public-safe',
      rate_label: 'Курс ЦБ РФ:',
      rate_note: '(используется для конвертации в рублях; обновляется раз в 12 часов)',
      ds_loading: 'проверяю снимок…',
      ds_live: 'LIVE · обновлено',
      ds_stale: 'СНИМОК',
      ds_demo: 'ДЕМО · моки',
      period_label: 'ПЕРИОД',
      period_today: 'сегодня',
      period_7d: '7 дней',
      period_30d: '30 дней',
      period_60d: '60 дней',
      period_all: 'всё время',
      method_summary: 'методология и приватность',
      method_sources_title: 'источники',
      method_sources_body: 'Claude Code hook, Codex local sessions/wrapper, OpenClaw JSONL и OpenCode SQLite. В snapshot попадает только учётная метаинформация: модель, токены, стоимость, время и агрегаты.',
      method_money_title: 'деньги',
      method_money_body: 'API-эквивалент считается по фактической стоимости провайдера или тарифам API. Подписки делятся на 30 дней. Чистая экономия = <code>API-эквивалент − подписки</code>.',
      method_time_title: 'время',
      method_time_body: 'Активное время — промежутки между событиями до 2 минут. «Без ИИ» — оценка трудоёмкости сессии через отдельный estimator плюс ручные правки. Codex/OpenClaw/OpenCode не удваивают productivity.',
      method_stress_title: 'стресс',
      method_stress_body: 'Мат считается только в пользовательских сообщениях. Недовольство и благодарность — агрегированная оценка сессий; тексты сообщений на страницу не выводятся.',
      method_privacy_title: 'публикация',
      method_privacy_body: 'Публичный режим хеширует session id, вырезает пути, email, токены и слова из blocklist. Перед публикацией snapshot дополнительно проверяется на персональные строки.',
      p_today: 'СЕГОДНЯ',
      today_calls: 'вызовов',
      today_cost: 'API-эквив.',
      today_active: 'активно',
      today_profanity: 'недовольство',
      today_top_session: 'главная сессия',
      p_combined: 'ЧИСТАЯ ЭКОНОМИЯ',
      combined_sub: 'API-эквивалент минус подписки за',
      combined_api_eq: 'API-эквивалент:',
      combined_subscriptions: 'подписки:',
      p_savings: 'ФИНАНСОВАЯ ФОРМУЛА',
      savings_formula_sub: 'расшифровка главного KPI за',
      api_eq: 'Эквивалент по API:',
      subscription: 'Подписка:',
      net_savings: 'Чистая экономия:',
      ratio: 'Окупаемость:',
      p_multi: 'МНОЖИТЕЛЬ ПРОИЗВОДИТЕЛЬНОСТИ',
      multi_sub: 'чистое время с ИИ против ручной оценки',
      with_ai: 'С ИИ:',
      without_ai: 'Без ИИ (оценка):',
      hours_saved: 'Сэкономлено часов:',
      p_active: 'АКТИВНОЕ ВРЕМЯ',
      active_sub: 'считаются паузы между сообщениями короче 2 минут',
      cal_span: 'Календарный диапазон:',
      sess_covered: 'Сессий оценено:',
      avg_day: 'В среднем в день:',
      p_budget: '5-ЧАСОВОЙ БЮДЖЕТ · НОРМА',
      tokens_used: 'токенов использовано',
      limit_max5x: 'лимит Max 5×',
      percent: '% от Max 5×',
      cache_tokens: 'кэш-токены вне лимита',
      vs_max20: '% от Max 20×',
      budget_status: 'статус',
      p_sentiment: 'ТОНАЛЬНОСТЬ · УРОВЕНЬ СТРЕССА',
      frustration: 'недовольство',
      appreciation: 'благодарность',
      prof_total: 'матов всего:',
      prof_units: 'матов',
      top_day: 'пиковый день:',
      stress_trend: 'тренд стресса:',
      trend_improving: 'улучшение',
      trend_worsening: 'ухудшение',
      trend_stable: 'стабильно',
      trend_na: 'н/д',
      p_models: 'МОДЕЛИ · РАСПРЕДЕЛЕНИЕ',
      p_sessions: 'ПОСЛЕДНИЕ СЕССИИ',
      p_timeline_prefix: 'ХРОНОЛОГИЯ АКТИВНОСТИ',
      tl_period: 'период',
      tl_total: 'всего событий',
      tl_peak: 'пиковый день'
    },
    en: {
      status: 'TRACKER · LIVE SNAPSHOT · LOCAL DATA',
      status_live: 'LIVE',
      unit_hours_short: ' h',
      mode_burst: 'OVER LIMIT',
      mode_normal: 'WITHIN LIMITS',
      phase_short: 'P',
      calls_unit: 'calls',
      calls_short: 'calls',
      cap_max5x_title: 'subscription cap: Max 5×',
      cap_max20x_title: 'subscription cap: Max 20×',
      mood_stable: 'stable',
      mood_calm: 'calm',
      mood_frustrated_calm: 'annoyed → calm',
      meta: '4 SOURCES · 3 MEASUREMENT AXES · PUBLIC-READY SNAPSHOT',
      hero_sub: 'OPERATIONS LOG. Every session leaves three numbers: <code>money</code> — API equivalent and subscription cost, <code>hours</code> — active time versus manual estimate, <code>stress</code> — aggregated emotional trace. The page shows an anonymized snapshot without prompt or response text.',
      hero_badge_sources: 'sources',
      hero_badge_sessions: 'sessions scored',
      hero_badge_multiplier: 'multiplier',
      hero_badge_savings: 'net savings',
      hero_badge_privacy: 'publishing',
      hero_badge_privacy_value: 'public-safe',
      rate_label: 'CBR rate:',
      rate_note: '(used for RUB conversion; refreshed every 12 hours)',
      ds_loading: 'checking snapshot…',
      ds_live: 'LIVE · updated',
      ds_stale: 'SNAPSHOT',
      ds_demo: 'DEMO · mock data',
      period_label: 'PERIOD',
      period_today: 'today',
      period_7d: '7 days',
      period_30d: '30 days',
      period_60d: '60 days',
      period_all: 'all time',
      method_summary: 'methodology and privacy',
      method_sources_title: 'sources',
      method_sources_body: 'Claude Code hook, Codex local sessions/wrapper, OpenClaw JSONL, and OpenCode SQLite. The snapshot stores accounting metadata only: model, tokens, cost, timestamps, and aggregates.',
      method_money_title: 'money',
      method_money_body: 'API equivalent uses actual provider cost when available or API rate estimates. Subscriptions are prorated over 30 days. Net savings = <code>API equivalent − subscriptions</code>.',
      method_time_title: 'time',
      method_time_body: 'Active time is built from event gaps up to 2 minutes. “Without AI” is a per-session baseline from a separate estimator plus manual corrections. Codex/OpenClaw/OpenCode do not double-count productivity.',
      method_stress_title: 'stress',
      method_stress_body: 'Profanity is counted only in user messages. Frustration and appreciation are aggregate session estimates; message text is never rendered on this page.',
      method_privacy_title: 'publishing',
      method_privacy_body: 'Public mode hashes session ids and strips paths, emails, tokens, and blocklist terms. Before publishing, the snapshot is scanned again for personal strings.',
      p_today: 'TODAY',
      today_calls: 'calls',
      today_cost: 'API equiv.',
      today_active: 'active',
      today_profanity: 'frustration',
      today_top_session: 'top session',
      p_combined: 'NET SAVINGS',
      combined_sub: 'API equivalent minus subscriptions over',
      combined_api_eq: 'API equivalent:',
      combined_subscriptions: 'subscriptions:',
      p_savings: 'FINANCIAL FORMULA',
      savings_formula_sub: 'main KPI breakdown over',
      api_eq: 'API equivalent:',
      subscription: 'Subscription:',
      net_savings: 'Net savings:',
      ratio: 'Payoff ratio:',
      p_multi: 'PRODUCTIVITY MULTIPLIER',
      multi_sub: 'clean AI time vs manual estimate',
      with_ai: 'With AI:',
      without_ai: 'Without AI (est):',
      hours_saved: 'Hours saved:',
      p_active: 'ACTIVE TIME',
      active_sub: 'gaps between messages under 2 minutes count',
      cal_span: 'Calendar span:',
      sess_covered: 'Sessions covered:',
      avg_day: 'Avg/day:',
      p_budget: '5-HOUR BUDGET · WITHIN LIMITS',
      tokens_used: 'tokens used',
      limit_max5x: 'Max 5× cap',
      percent: '% of Max 5×',
      cache_tokens: 'cache tokens outside cap',
      vs_max20: '% of Max 20×',
      budget_status: 'status',
      p_sentiment: 'SENTIMENT · STRESS METER',
      frustration: 'frustration',
      appreciation: 'appreciation',
      prof_total: 'profanity total:',
      prof_units: 'swears',
      top_day: 'peak day:',
      stress_trend: 'stress trend:',
      trend_improving: 'improving',
      trend_worsening: 'worsening',
      trend_stable: 'stable',
      trend_na: 'n/a',
      p_models: 'MODELS · DISTRIBUTION',
      p_sessions: 'RECENT SESSIONS',
      p_timeline_prefix: 'ACTIVITY TIMELINE',
      tl_period: 'period',
      tl_total: 'total events',
      tl_peak: 'peak day'
    }
  };

  function getCurrentLang() {
    if (window.i18n && window.i18n.lang) return window.i18n.lang;
    const url = new URL(location.href);
    const urlLang = url.searchParams.get('lang');
    if (urlLang === 'en' || urlLang === 'ru') return urlLang;
    try {
      const stored = localStorage.getItem('androman-lang');
      if (stored === 'en' || stored === 'ru') return stored;
    } catch (e) {}
    return 'ru';
  }

  function fmtMoney(usd, lang, decimals) {
    if (decimals === undefined) decimals = (Math.abs(usd) < 100 ? 2 : 0);
    if (lang === 'en') {
      return '$' + Number(usd).toLocaleString('en-US', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals
      });
    }
    const rub = usd * RATE;
    return Number(rub).toLocaleString('ru-RU', {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0
    }) + ' ₽';
  }

  function applyMaTranslations() {
    const lang = getCurrentLang();
    const dict = TRANSLATIONS[lang] || TRANSLATIONS.ru;

    // Text translations
    document.querySelectorAll('[data-ma-i18n]').forEach(function(el) {
      const key = el.dataset.maI18n;
      if (dict[key]) el.textContent = dict[key];
    });

    // HTML translations (для блоков с code/inline tags)
    document.querySelectorAll('[data-ma-i18n-html]').forEach(function(el) {
      const key = el.dataset.maI18nHtml;
      if (dict[key]) el.innerHTML = dict[key];
    });

    // Per-element session description (бытовой случай — alternate text без dict)
    document.querySelectorAll('[data-ma-i18n-text]').forEach(function(el) {
      const enText = el.dataset.maI18nText;
      const ruText = el.dataset.maI18nTextRu || el.textContent;
      // Сохраняем оригинал ru на первом проходе
      if (!el.dataset.maI18nTextRu) {
        el.dataset.maI18nTextRu = el.textContent;
      }
      el.textContent = (lang === 'en') ? enText : el.dataset.maI18nTextRu;
    });

    // Title attribute translations
    document.querySelectorAll('[data-ma-i18n-title]').forEach(function(el) {
      const key = el.dataset.maI18nTitle;
      if (dict[key]) el.title = dict[key];
    });

    // Currency conversion
    document.querySelectorAll('[data-usd]').forEach(function(el) {
      const usd = parseFloat(el.dataset.usd);
      if (isNaN(usd)) return;
      const decimals = el.dataset.decimals !== undefined ? parseInt(el.dataset.decimals) : undefined;
      el.textContent = fmtMoney(usd, lang, decimals);
    });
  }

  // === Period filter ===
  // DAYS_TOTAL is mutable: PHP-baked default, but reset from snapshot.totals.days on load
  // so a 30-day snapshot doesn't try to render 62 bars or hide everything.
  let DAYS_TOTAL = <?php echo (int) $totals['days']; ?>;
  const PERIOD_BASE = { today: 1, '7d': 7, '30d': 30, '60d': 60 };

  function periodToDays(key) {
    if (key === 'all') return DAYS_TOTAL;
    const n = PERIOD_BASE[key];
    if (n === undefined) return DAYS_TOTAL;
    return Math.min(n, DAYS_TOTAL);
  }

  function ruDaysNoun(n) {
    const last2 = n % 100;
    if (last2 >= 11 && last2 <= 14) return 'дней';
    const last = n % 10;
    if (last === 1) return 'день';
    if (last >= 2 && last <= 4) return 'дня';
    return 'дней';
  }

  function fmtNumberRu(n, decimals) {
    const fixed = (decimals && decimals > 0) ? n.toFixed(decimals) : Math.round(n).toString();
    const [whole, frac] = fixed.split('.');
    const groups = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    return frac ? groups + '.' + frac : groups;
  }

  function applyPeriod(periodKey) {
    const N = periodToDays(periodKey);
    const ratio = DAYS_TOTAL > 0 ? N / DAYS_TOTAL : 1;
    const useToday = (periodKey === 'today');
    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';

    // resolve(el, kind) → today-override if useToday & data-today-* present, else base × ratio
    function resolve(el, kind) {
      const todayAttr = el.dataset['today' + kind.charAt(0).toUpperCase() + kind.slice(1)];
      if (useToday && todayAttr !== undefined) return parseFloat(todayAttr);
      const base = parseFloat(el.dataset['base' + kind.charAt(0).toUpperCase() + kind.slice(1)]);
      return isNaN(base) ? null : base * ratio;
    }

    // Timeline bars: hide outside last-N window
    document.querySelectorAll('.ma-tl-bar').forEach(function(bar) {
      const idx = parseInt(bar.dataset.dayIdx, 10);
      bar.style.display = idx >= (DAYS_TOTAL - N) ? '' : 'none';
    });

    // Day-count labels + Russian/English declension
    const noun = lang === 'en' ? (N === 1 ? 'day' : 'days') : ruDaysNoun(N);
    document.querySelectorAll('[data-period-days]').forEach(el => el.textContent = N);
    document.querySelectorAll('[data-period-days-noun]').forEach(el => el.textContent = noun);
    document.querySelectorAll('[data-period-days-noun-up]').forEach(el => el.textContent = noun.toUpperCase());

    // USD values
    document.querySelectorAll('[data-base-usd]').forEach(function(el) {
      const v = resolve(el, 'usd');
      if (v === null) return;
      el.dataset.usd = v.toFixed(4);
    });

    // Hours values
    document.querySelectorAll('[data-base-hours]').forEach(function(el) {
      const v = resolve(el, 'hours');
      if (v === null) return;
      const decimals = el.dataset.decimals !== undefined ? parseInt(el.dataset.decimals, 10) : 1;
      el.textContent = fmtNumberRu(v, decimals);
    });

    // Integer counts
    document.querySelectorAll('[data-base-int]').forEach(function(el) {
      const v = resolve(el, 'int');
      if (v === null) return;
      el.textContent = fmtNumberRu(Math.round(v), 0);
    });

    // Derived multiplier: (with-AI + saved) / with-AI → 1 + saved/withAI
    const withEl = document.querySelector('[data-multi-input="with-ai"]');
    const savedEl = document.querySelector('[data-multi-input="saved"]');
    const multiDisplay = document.querySelector('[data-multi-display]');
    if (withEl && savedEl && multiDisplay) {
      const withAI = resolve(withEl, 'hours') || 0;
      const saved = resolve(savedEl, 'hours') || 0;
      if (withAI > 0) {
        const m = 1 + saved / withAI;
        multiDisplay.textContent = fmtNumberRu(m, 1);
      }
    }

    // Derived avg/day in active panel: today=today.active_hours/1 = 1.4; otherwise active_total/N
    const avgEl = document.querySelector('[data-avg-display]');
    if (avgEl && withEl) {
      const withAI = resolve(withEl, 'hours') || 0;
      const avgPerDay = withAI / Math.max(1, N);
      avgEl.textContent = fmtNumberRu(avgPerDay, 1);
    }

    // Derived ratio (Окупаемость / ROI): api_eq / subscription
    const apiEl = document.querySelector('[data-ratio-input="api"]');
    const subEl = document.querySelector('[data-ratio-input="sub"]');
    const ratioEl = document.querySelector('[data-ratio-display]');
    if (apiEl && subEl && ratioEl) {
      const api = parseFloat(apiEl.dataset.usd);
      const sub = parseFloat(subEl.dataset.usd);
      if (sub > 0 && !isNaN(api)) {
        const r = api / sub;
        ratioEl.textContent = '×' + (Math.abs(r) >= 10 ? Math.round(r) : r.toFixed(2));
      }
    }

    // Re-run i18n + currency conversion to refresh USD displays after data-usd update
    // Must run BEFORE updateMultiPanelLabel — applyMaTranslations rewrites
    // .big-number-sub from the dict (data-ma-i18n="multi_sub"), and we need
    // to append the period-suffix after that.
    applyMaTranslations();
    updateBudgetStatus(SNAPSHOT_DATA);
    setSnapSentiment(SNAPSHOT_DATA);

    // Filter sessions panel rows so only those active in the selected
    // period are visible (Codex review A — sessions panel was leaking
    // older sessions when user picked "today").
    filterSessionsByPeriod(periodKey);

    // Period-aware productivity uses backend-computed session subsets when
    // available, so the multiplier reflects the selected period.
    updateProductivityPanelForPeriod(periodKey);
  }

  function bindPeriodSelector() {
    const buttons = document.querySelectorAll('.ma-period-btn');
    buttons.forEach(function(btn) {
      if (btn.dataset.maPeriodBound) return;
      btn.dataset.maPeriodBound = '1';
      btn.addEventListener('click', function(ev) {
        ev.preventDefault();
        const group = btn.closest('.ma-period-bar');
        if (!group) return;
        group.querySelectorAll('.ma-period-btn').forEach(function(item) {
          const isActive = item === btn;
          item.classList.toggle('ma-active', isActive);
          item.setAttribute('aria-pressed', isActive ? 'true' : 'false');
        });
        applyPeriod(btn.dataset.period);
      });
    });
  }

  function getInitialPeriod() {
    const active = document.querySelector('.ma-period-btn.ma-active');
    return active ? active.dataset.period : 'today';
  }

  // === Snapshot loader (Phase 3.5) ==========================================
  // Backend writes WP-shaped JSON to /wp-content/uploads/multi-agent/snapshot.json
  // every 15 min (atomic). On load we fetch and override scalar fields via
  // data-snap-* markers; rebuild timeline + models/sessions lists; update status
  // badge. Missing/old snapshot → silent fallback to PHP-baked mock.

  // Latest status — kept so language switch can re-render the badge text.
  let SNAPSHOT_STATE = { status: 'loading', hint: '' };
  // Latest snapshot payload — kept so applyPeriod can derive period-specific
  // metrics (e.g. today multi from snap.today instead of pro-rate).
  let SNAPSHOT_DATA = null;

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, function(c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtIntRu(n) {
    return Number(n || 0).toLocaleString('ru-RU').replace(/,/g, ' ');
  }

  function timelineLabels() {
    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';
    return (lang === 'en')
      ? { calls: 'calls', cost: 'API equiv.', active: 'active', input: 'input', output: 'output', cache: 'cache', hours: 'h', claude: 'Claude', codex: 'Codex', openclaw: 'OpenClaw', opencode: 'OpenCode' }
      : { calls: 'вызовы', cost: 'API-эквив.', active: 'активно', input: 'вход', output: 'выход', cache: 'кэш', hours: 'ч', claude: 'Claude', codex: 'Codex', openclaw: 'OpenClaw', opencode: 'OpenCode' };
  }

  function timelineProviderCalls(day, key) {
    const providers = day && day.providers ? day.providers : {};
    const p = providers[key] || {};
    return Number(p.calls || 0);
  }

  function timelineTooltipHtml(day) {
    const l = timelineLabels();
    const cost = fmtMoney(Number(day.cost_usd || 0), getCurrentLang(), 2);
    return '<div class="tt-date">' + escapeHtml(day.date || '') + '</div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.calls) + '</span><span class="tt-v">' + fmtIntRu(day.calls || 0) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.cost) + '</span><span class="tt-v">' + escapeHtml(cost) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.active) + '</span><span class="tt-v">' + fmtNumberRu(Number(day.active_hours || 0), 1) + ' ' + escapeHtml(l.hours) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.input) + '</span><span class="tt-v">' + fmtIntRu(day.input_tokens || 0) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.output) + '</span><span class="tt-v">' + fmtIntRu(day.output_tokens || 0) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.cache) + '</span><span class="tt-v">' + fmtIntRu(day.cache_tokens || 0) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.claude) + '</span><span class="tt-v">' + fmtIntRu(timelineProviderCalls(day, 'anthropic_claude')) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.codex) + '</span><span class="tt-v">' + fmtIntRu(timelineProviderCalls(day, 'openai_codex')) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.openclaw) + '</span><span class="tt-v">' + fmtIntRu(timelineProviderCalls(day, 'openrouter_openclaw')) + '</span></div>'
      + '<div class="tt-row"><span class="tt-k">' + escapeHtml(l.opencode) + '</span><span class="tt-v">' + fmtIntRu(timelineProviderCalls(day, 'opencode_openrouter')) + '</span></div>';
  }

  function timelineTooltipTitle(day) {
    const l = timelineLabels();
    return String(day.date || '') + ': ' + fmtIntRu(day.calls || 0) + ' ' + l.calls + ', '
      + fmtMoney(Number(day.cost_usd || 0), getCurrentLang(), 2);
  }

  function getTimelineTooltip() {
    let tip = document.querySelector('.ma-tl-tooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'ma-tl-tooltip';
      document.body.appendChild(tip);
    }
    return tip;
  }

  function positionTimelineTooltip(ev, anchor) {
    const tip = document.querySelector('.ma-tl-tooltip');
    if (!tip) return;
    const margin = 14;
    const rect = tip.getBoundingClientRect();
    let x = Number(ev && ev.clientX);
    let y = Number(ev && ev.clientY);
    if ((!Number.isFinite(x) || !Number.isFinite(y)) && anchor && anchor.getBoundingClientRect) {
      const a = anchor.getBoundingClientRect();
      x = a.left + a.width / 2;
      y = a.top;
    }
    let left = x + margin;
    let top = y + margin;
    if (left + rect.width + margin > window.innerWidth) left = x - rect.width - margin;
    if (top + rect.height + margin > window.innerHeight) top = y - rect.height - margin;
    tip.style.left = Math.max(margin, left) + 'px';
    tip.style.top = Math.max(margin, top) + 'px';
  }

  function showTimelineTooltip(ev) {
    const html = this.dataset.tipHtml;
    if (!html) return;
    const tip = getTimelineTooltip();
    tip.innerHTML = html;
    tip.classList.add('visible');
    positionTimelineTooltip(ev, this);
  }

  function moveTimelineTooltip(ev) {
    positionTimelineTooltip(ev, this);
  }

  function hideTimelineTooltip() {
    const tip = document.querySelector('.ma-tl-tooltip');
    if (tip) tip.classList.remove('visible');
  }

  function setSnapshotStatus(status, hint) {
    SNAPSHOT_STATE = { status: status, hint: hint || '' };
    renderSnapshotBadge();
  }

  function renderSnapshotBadge() {
    const el = document.querySelector('.ma-data-source');
    if (!el) return;
    el.dataset.snapshotStatus = SNAPSHOT_STATE.status;
    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';
    const dict = TRANSLATIONS[lang] || TRANSLATIONS.ru;
    const hint = SNAPSHOT_STATE.hint;
    let label = '';
    if (SNAPSHOT_STATE.status === 'live') label = dict.ds_live + (hint ? ' ' + hint : '');
    else if (SNAPSHOT_STATE.status === 'stale') label = dict.ds_stale + (hint ? ' ' + hint : '');
    else if (SNAPSHOT_STATE.status === 'demo') label = dict.ds_demo;
    else label = dict.ds_loading;
    el.textContent = label;
  }

  function rebuildTimelineFromWeights(weights, days) {
    if (!Array.isArray(weights) || weights.length === 0) return;
    const container = document.querySelector('.ma-timeline');
    if (!container) return;
    const max = Math.max.apply(null, weights.concat([1]));

    // Always rebuild — DAYS_TOTAL may have changed (e.g. snapshot=30 vs PHP-baked=62)
    container.innerHTML = '';
    weights.forEach(function(w, i) {
      const h = (w / max) * 100;
      const bar = document.createElement('div');
      bar.className = 'ma-tl-bar';
      bar.dataset.dayIdx = String(i);
      bar.dataset.dayCalls = String(w);
      const day = Array.isArray(days) ? days[i] : null;
      if (day) {
        bar.dataset.date = String(day.date || '');
        bar.dataset.tipHtml = timelineTooltipHtml(day);
        bar.title = timelineTooltipTitle(day);
        bar.addEventListener('mouseenter', showTimelineTooltip);
        bar.addEventListener('mousemove', moveTimelineTooltip);
        bar.addEventListener('mouseleave', hideTimelineTooltip);
        bar.addEventListener('focus', showTimelineTooltip);
        bar.addEventListener('blur', hideTimelineTooltip);
        bar.tabIndex = 0;
      } else {
        bar.title = String(w);
      }
      const fill = document.createElement('div');
      fill.className = 'ma-tl-fill';
      fill.style.height = h + '%';
      bar.appendChild(fill);
      container.appendChild(bar);
    });
  }

  function rebuildModelsList(models) {
    if (!Array.isArray(models)) return;
    const container = document.querySelector('[data-snap-models-list]');
    if (!container) return;
    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';
    container.innerHTML = '';
    models.forEach(function(m) {
      const row = document.createElement('div');
      row.className = 'bar-row';
      row.innerHTML = '<span class="bar-label">' + escapeHtml(m.name) + '</span>'
        + '<span class="bar-track"><span class="bar-fill" style="width: '
        + Math.max(0, Math.min(100, parseFloat(m.pct) || 0)) + '%;"></span></span>'
        + '<span class="bar-cnt" data-usd="' + (parseFloat(m.cost) || 0) + '"></span>';
      container.appendChild(row);
    });
  }

  function rebuildSessionsList(sessions) {
    if (!Array.isArray(sessions)) return;
    const container = document.querySelector('[data-snap-sessions-list]');
    if (!container) return;
    container.innerHTML = '';
    sessions.forEach(function(s) {
      const row = document.createElement('div');
      row.className = 'sess-row';
      // Session id short — privacy-soft (8 hex) since session_id is internal UUID;
      // for public publish (Phase 4) salt+hash will be applied backend-side.
      const sid = (s.session_id_short || '').slice(0, 8);
      const desc = (s.desc || '').trim();
      const moodKey = (s.mood || '').replace(/→/g, '_').replace(/\s/g, '_');
      const dollarsAttr = parseFloat(s.cost_usd) || 0;
      // Filter-by-period anchor: row's last-event timestamp.
      if (s.last_ts) row.dataset.lastTs = s.last_ts;
      if (s.first_ts) row.dataset.firstTs = s.first_ts;
      row.innerHTML = '<span class="sp">' + escapeHtml(sid) + '</span>'
        + '<span class="sd">' + escapeHtml(desc)
        + (moodKey ? ' <span class="mood" data-ma-i18n="mood_' + escapeHtml(moodKey) + '">' + escapeHtml(s.mood || '') + '</span>' : '')
        + '</span>'
        + '<span class="sc" data-usd="' + dollarsAttr + '" data-decimals="2"></span>';
      container.appendChild(row);
    });
  }

  // Multi-panel period-aware metric & subtitle.
  // Prefer backend-computed productivity_periods: pro-rating active/saved hours
  // makes the multiplier artificially constant. Fallback to legacy labels when
  // an older snapshot is loaded.
  const MULTI_TODAY_MIN_ACTIVE_H = 0.5;

  function finiteNumber(value) {
    const n = parseFloat(value);
    return Number.isFinite(n) ? n : null;
  }

  function setHoursText(selector, value, decimals) {
    const el = document.querySelector(selector);
    if (!el) return;
    const n = finiteNumber(value);
    el.textContent = (n === null) ? '—' : fmtNumberRu(n, decimals);
  }

  function setPlainText(selector, value) {
    const el = document.querySelector(selector);
    if (el) el.textContent = value;
  }

  function productivityPeriodFor(periodKey) {
    const periods = SNAPSHOT_DATA && SNAPSHOT_DATA.productivity_periods;
    if (periods && periods[periodKey]) return periods[periodKey];
    if (periodKey === 'all' && SNAPSHOT_DATA && SNAPSHOT_DATA.productivity) {
      return SNAPSHOT_DATA.productivity;
    }
    return null;
  }

  function updateMultiPanelLabel(periodKey) {
    const display = document.querySelector('[data-multi-display]');
    const sub = document.querySelector('.p-multi .big-number-sub');
    if (!display || !sub) return;
    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';
    const dict = TRANSLATIONS[lang] || TRANSLATIONS.ru;

    const tIsToday = (periodKey === 'today');
    let baseSubtitle = dict.multi_sub || 'чистое время с ИИ против ручной оценки';
    let suffix = '';

    if (tIsToday && SNAPSHOT_DATA && SNAPSHOT_DATA.today) {
      const t = SNAPSHOT_DATA.today;
      const activeFE = parseFloat(t.active_hours_for_estimate);
      const saved = parseFloat(t.hours_saved);
      if (Number.isFinite(activeFE) && activeFE >= MULTI_TODAY_MIN_ACTIVE_H && saved >= 0) {
        const m = 1 + saved / activeFE;
        display.textContent = fmtNumberRu(m, 1);
        suffix = (lang === 'en') ? ' (today)' : ' (за сегодня)';
      } else {
        // Not enough today coverage to compute a meaningful multi
        display.textContent = '—';
        suffix = (lang === 'en')
          ? ' (not enough today coverage)'
          : ' (мало данных за сегодня)';
        sub.textContent = baseSubtitle + suffix;
        return;
      }
    } else {
      suffix = (lang === 'en') ? ' (cumulative)' : ' (за весь период)';
    }
    sub.textContent = baseSubtitle + suffix;
  }

  function updateProductivityPanelForPeriod(periodKey) {
    const p = productivityPeriodFor(periodKey);
    if (!p) {
      updateMultiPanelLabel(periodKey);
      return;
    }

    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';
    const dict = TRANSLATIONS[lang] || TRANSLATIONS.ru;
    const days = Math.max(1, parseInt(p.days || periodToDays(periodKey), 10));
    const activeDisplay = finiteNumber(p.active_hours);
    const estimateActive = finiteNumber(
      p.estimate_active_hours != null ? p.estimate_active_hours : p.active_hours
    );
    const saved = finiteNumber(p.hours_saved);
    let estimated = finiteNumber(p.estimated_hours);
    if (estimated === null && estimateActive !== null && saved !== null) {
      estimated = estimateActive + saved;
    }
    let multiplier = finiteNumber(p.multiplier);
    if ((multiplier === null || multiplier <= 0) && estimateActive !== null && estimateActive > 0 && estimated !== null) {
      multiplier = estimated / estimateActive;
    }

    setHoursText('[data-snap-with-ai]', estimateActive, 1);
    setHoursText('[data-snap-without-ai]', estimated, 1);
    setHoursText('[data-snap-hours-saved]', saved, 1);
    setHoursText('[data-snap-active-hours]', activeDisplay, 1);
    setHoursText('[data-snap-calendar-hours]', p.calendar_hours, 0);

    if (activeDisplay !== null) {
      setPlainText('[data-avg-display]', fmtNumberRu(activeDisplay / days, 1));
    }
    if (p.sessions_covered != null) {
      setPlainText('[data-snap-productivity="sessions_covered"]', fmtNumberRu(parseInt(p.sessions_covered, 10) || 0, 0));
    }
    if (p.sessions_total != null) {
      setPlainText('[data-snap-productivity="sessions_total"]', fmtNumberRu(parseInt(p.sessions_total, 10) || 0, 0));
    }

    const display = document.querySelector('[data-multi-display]');
    const sub = document.querySelector('.p-multi .big-number-sub');
    if (!display || !sub) return;

    const baseSubtitle = dict.multi_sub || 'чистое время с ИИ против ручной оценки';
    const isUnstableToday = periodKey === 'today'
      && (estimateActive === null || estimateActive < MULTI_TODAY_MIN_ACTIVE_H);

    if (isUnstableToday || multiplier === null || multiplier <= 0) {
      display.textContent = '—';
      sub.textContent = baseSubtitle + (
        lang === 'en' ? ' (not enough coverage)' : ' (мало оцененных данных)'
      );
      return;
    }

    display.textContent = fmtNumberRu(multiplier, 1);
    const suffix = (lang === 'en')
      ? (periodKey === 'today' ? ' (today)' : ' (selected period)')
      : (periodKey === 'today' ? ' (за сегодня)' : ' (за выбранный период)');
    sub.textContent = baseSubtitle + suffix;
  }

  // Filter sessions panel rows by selected period — last_ts within window.
  function filterSessionsByPeriod(periodKey) {
    let cutoffMs;
    if (periodKey === 'today') {
      const midnight = new Date();
      midnight.setHours(0, 0, 0, 0);
      cutoffMs = midnight.getTime();
    } else if (periodKey === 'all') {
      cutoffMs = 0;  // show everything
    } else {
      const N = periodToDays(periodKey);
      cutoffMs = Date.now() - N * 86400000;
    }
    let visible = 0;
    document.querySelectorAll('.sess-row').forEach(function(row) {
      const tsRaw = row.dataset.lastTs;
      if (!tsRaw) {
        // PHP-baked rows (before snapshot loaded) — keep visible
        row.style.display = '';
        visible++;
        return;
      }
      const ts = new Date(tsRaw).getTime();
      const inWindow = Number.isFinite(ts) && ts >= cutoffMs;
      row.style.display = inWindow ? '' : 'none';
      if (inWindow) visible++;
    });
    // Optional: show "no sessions in this period" hint when empty
    const container = document.querySelector('[data-snap-sessions-list]');
    if (container) {
      let empty = container.querySelector('.sess-empty');
      if (visible === 0) {
        if (!empty) {
          empty = document.createElement('div');
          empty.className = 'sess-empty';
          empty.style.cssText = 'padding: 8px 0; color: var(--text-muted); font-size: 11px; letter-spacing: 0.1em;';
          empty.textContent = '— нет сессий в этом окне —';
          container.appendChild(empty);
        }
        empty.style.display = '';
      } else if (empty) {
        empty.style.display = 'none';
      }
    }
  }

  function setSnapBudget(snap) {
    if (!snap || !snap.budget) return;
    const b = snap.budget;
    document.querySelectorAll('[data-snap-budget]').forEach(function(el) {
      const k = el.dataset.snapBudget;
      if (b[k] != null) el.textContent = fmtIntRu(b[k]);
    });
    document.querySelectorAll('[data-snap-budget-pct]').forEach(function(el) {
      const k = el.dataset.snapBudgetPct;
      if (b[k] != null) el.textContent = Math.round(b[k]) + '%';
    });
    const fill = document.querySelector('.budget-fill');
    if (fill && b.percent_20x != null) {
      fill.style.width = Math.min(100, b.percent_20x) + '%';
    }
    updateBudgetStatus(snap);
  }

  function updateBudgetStatus(snap) {
    if (!snap || !snap.budget) return;
    const b = snap.budget;
    const used = Number(b.tokens_used || 0);
    const limit = Number(b.limit_5h || 0);
    const pct = Number(b.percent_5x || 0);
    const over = (pct > 100) || (limit > 0 && used > limit);
    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';
    const dict = TRANSLATIONS[lang] || TRANSLATIONS.ru;
    const stateKey = over ? 'mode_burst' : 'mode_normal';
    const stateText = dict[stateKey] || (over ? 'ПЕРЕРАСХОД' : 'НОРМА');

    const title = document.querySelector('.p-budget .ptitle');
    if (title) {
      title.textContent = (lang === 'en')
        ? '5-HOUR BUDGET · ' + stateText
        : '5-ЧАСОВОЙ БЮДЖЕТ · ' + stateText;
    }

    const status = document.querySelector('.p-budget [data-ma-i18n="mode_normal"], .p-budget [data-ma-i18n="mode_burst"]');
    if (status) {
      status.dataset.maI18n = stateKey;
      status.textContent = stateText;
      status.classList.toggle('hot', over);
    }
  }

  function setSnapSentiment(snap) {
    if (!snap || !snap.sentiment) return;
    const s = snap.sentiment;
    document.querySelectorAll('[data-snap-sent]').forEach(function(el) {
      const k = el.dataset.snapSent;
      if (s[k] != null) el.textContent = String(s[k]);
    });
    document.querySelectorAll('[data-snap-sent-row]').forEach(function(row) {
      const k = row.dataset.snapSentRow;
      const v = s[k];
      if (v == null) return;
      const fill = row.querySelector('.sent-fill');
      const val = row.querySelector('.sent-val');
      if (fill) fill.style.width = Math.round(parseFloat(v) * 100) + '%';
      if (val) val.textContent = parseFloat(v).toFixed(2);
    });
    const td = s.top_day || {};
    const dEl = document.querySelector('[data-snap-sent-topday-date]');
    const pEl = document.querySelector('[data-snap-sent-topday-prof]');
    if (dEl && td.date) dEl.textContent = td.date;
    if (pEl && td.profanity != null) pEl.textContent = String(td.profanity);

    const trendEl = document.querySelector('[data-snap-sent-stress-trend]');
    if (trendEl && s.stress_trend) {
      trendEl.textContent = formatStressTrend(s.stress_trend);
    }
  }

  function formatStressTrend(value) {
    const lang = (typeof getCurrentLang === 'function') ? getCurrentLang() : 'ru';
    const dict = TRANSLATIONS[lang] || TRANSLATIONS.ru;
    const v = String(value || '').toLowerCase();
    if (v === 'improving') return '↘ ' + (dict.trend_improving || 'улучшение');
    if (v === 'worsening') return '↗ ' + (dict.trend_worsening || 'ухудшение');
    if (v === 'stable') return '→ ' + (dict.trend_stable || 'стабильно');
    return dict.trend_na || 'н/д';
  }

  function applyProviders(snap) {
    if (!snap.providers || typeof snap.providers !== 'object') return;
    const todayProviders = (snap.today && snap.today.providers && typeof snap.today.providers === 'object')
      ? snap.today.providers
      : {};
    document.querySelectorAll('[data-snap-provider]').forEach(function(row) {
      const key = row.dataset.snapProvider;
      const p = snap.providers[key];
      if (!p || !p.calls) {
        row.style.display = 'none';
        return;
      }
      row.style.display = '';
      const calls = row.querySelector('.prov-calls');
      const cost = row.querySelector('.prov-cost');
      if (calls) {
        calls.dataset.baseInt = String(p.calls || 0);
        const pt = todayProviders[key];
        if (pt && pt.calls != null) calls.dataset.todayInt = String(pt.calls || 0);
        calls.textContent = fmtIntRu(p.calls || 0);
      }
      if (cost) {
        const usd = (p.api_equivalent_cost_usd != null) ? p.api_equivalent_cost_usd : p.cost_usd;
        cost.dataset.baseUsd = String(usd || 0);
        const pt = todayProviders[key];
        if (pt) {
          const todayUsd = (pt.api_equivalent_cost_usd != null) ? pt.api_equivalent_cost_usd : pt.cost_usd;
          if (todayUsd != null) cost.dataset.todayUsd = String(todayUsd || 0);
        }
      }
    });
  }

  function applyTotalsToBaseAttrs(snap) {
    if (!snap.totals) return;
    const t = snap.totals;
    const combinedSavings = (t.savings_usd != null) ? t.savings_usd : t.cost_usd_combined;
    if (combinedSavings != null) {
      const el = document.querySelector('.p-combined .big-number');
      if (el) el.dataset.baseUsd = String(combinedSavings);
    }
    if (t.cost_usd_combined != null || t.cost_usd != null) {
      const el = document.querySelector('[data-combined-api]');
      if (el) el.dataset.baseUsd = String(t.cost_usd_combined != null ? t.cost_usd_combined : t.cost_usd);
    }
    if (t.subscription_usd != null) {
      const el = document.querySelector('[data-combined-subscription]');
      if (el) el.dataset.baseUsd = String(t.subscription_usd);
    }
    if (t.savings_usd != null) {
      const el = document.querySelector('[data-savings-net]');
      if (el) el.dataset.baseUsd = String(t.savings_usd);
    }
    if (t.cost_usd != null) {
      const el = document.querySelector('[data-ratio-input="api"]');
      if (el) el.dataset.baseUsd = String(t.cost_usd);
    }
    if (t.subscription_usd != null) {
      const el = document.querySelector('[data-ratio-input="sub"]');
      if (el) el.dataset.baseUsd = String(t.subscription_usd);
    }
    if (t.calls != null) {
      const el = document.querySelector('[data-snap-total-calls]');
      if (el) el.dataset.baseInt = String(t.calls);
    }
  }

  function applyProductivityToBaseAttrs(snap) {
    if (!snap.productivity) return;
    const p = snap.productivity;

    // D1 (Codex review): all-or-none override for productivity fields.
    // If the productivity block is partial (multiplier clamped to 0 by the
    // backend because too few sessions are estimated), partially overriding
    // would mix live `with-ai` against PHP-baked `saved` → derived multiplier
    // becomes garbage like ×214. Better to keep the entire panel on mock
    // until productivity is fully real, OR override everything when it is.
    const productivityIsLive = (
      p.multiplier > 0 && p.active_hours > 0 && p.hours_saved > 0
    );

    // active_hours and calendar_hours are independent metrics — they're
    // valid even when the multiplier hasn't been derived yet.
    const sideMap = [
      ['snap-active-hours',   p.active_hours],
      ['snap-calendar-hours', p.calendar_hours],
    ];
    sideMap.forEach(function(pair) {
      const val = pair[1];
      if (val == null || val <= 0) return;
      const el = document.querySelector('[data-' + pair[0] + ']');
      if (el) el.dataset.baseHours = String(val);
    });

    document.querySelectorAll('[data-snap-productivity]').forEach(function(el) {
      const key = el.dataset.snapProductivity;
      const val = p[key];
      if (val != null) el.textContent = fmtIntRu(val);
    });

    if (!productivityIsLive) return;

    // Productivity fully live — override the multi-panel triple
    const multiMap = [
      ['snap-with-ai',        p.active_hours],
      ['snap-without-ai',     p.active_hours + p.hours_saved],
      ['snap-hours-saved',    p.hours_saved],
    ];
    multiMap.forEach(function(pair) {
      const el = document.querySelector('[data-' + pair[0] + ']');
      if (el) el.dataset.baseHours = String(pair[1]);
    });
  }

  function applyHeroBadges(snap) {
    if (!snap || typeof snap !== 'object') return;

    const providers = snap.providers || {};
    const providerKeys = Object.keys(providers);
    const providerCount = providerKeys.length;
    const p = (snap.productivity_periods && snap.productivity_periods.all)
      ? snap.productivity_periods.all
      : (snap.productivity || {});

    const sourcesEl = document.querySelector('[data-hero-provider-count]');
    if (sourcesEl && providerCount > 0) sourcesEl.textContent = fmtNumberRu(providerCount, 0);

    const sessionsCoveredEl = document.querySelector('[data-hero-sessions-covered]');
    if (sessionsCoveredEl && p.sessions_covered != null) {
      sessionsCoveredEl.textContent = fmtNumberRu(parseInt(p.sessions_covered, 10) || 0, 0);
    }
    const sessionsTotalEl = document.querySelector('[data-hero-sessions-total]');
    if (sessionsTotalEl && p.sessions_total != null) {
      sessionsTotalEl.textContent = fmtNumberRu(parseInt(p.sessions_total, 10) || 0, 0);
    }

    const multiplier = finiteNumber(p.multiplier);
    const multiplierEl = document.querySelector('[data-hero-multiplier]');
    if (multiplierEl && multiplier !== null && multiplier > 0) {
      multiplierEl.textContent = fmtNumberRu(multiplier, 1);
    }

    const savingsEl = document.querySelector('[data-hero-savings]');
    if (savingsEl && snap.totals && snap.totals.savings_usd != null) {
      savingsEl.dataset.usd = String(snap.totals.savings_usd);
    }
  }

  function applyTodayToTodayAttrs(snap) {
    if (!snap.today) return;
    const t = snap.today;
    // Pre-compute today's "savings" (negative if API < daily-prorated subscription)
    let todaySavings = null;
    let dailySub = null;
    if (t.cost_usd != null && snap.totals && snap.totals.subscription_usd != null) {
      dailySub = snap.totals.subscription_usd / Math.max(1, snap.totals.days || DAYS_TOTAL);
      todaySavings = Number((t.cost_usd - dailySub).toFixed(2));
    }
    if (todaySavings != null) {
      const el = document.querySelector('[data-savings-net]');
      if (el) el.dataset.todayUsd = String(todaySavings);
      const combined = document.querySelector('.p-combined .big-number');
      if (combined) combined.dataset.todayUsd = String(todaySavings);
    }
    if (t.cost_usd != null) {
      const el = document.querySelector('[data-ratio-input="api"]');
      if (el) el.dataset.todayUsd = String(t.cost_usd);
      const combinedApi = document.querySelector('[data-combined-api]');
      if (combinedApi) combinedApi.dataset.todayUsd = String(t.cost_usd);
    }
    if (dailySub != null) {
      const subEl = document.querySelector('[data-ratio-input="sub"]');
      if (subEl) subEl.dataset.todayUsd = String(dailySub);
      const combinedSub = document.querySelector('[data-combined-subscription]');
      if (combinedSub) combinedSub.dataset.todayUsd = String(dailySub);
    }
    // Today overrides only for the "АКТИВНО" headline + calendar span.
    // Multi panel intentionally pro-rates (skipping today-snapshot) — see PHP
    // comment in p-multi: per-session baselines vs fresh activity diverge.
    const hourMap = {
      'snap-active-hours':    t.active_hours,
      'snap-calendar-hours':  24,
    };
    Object.keys(hourMap).forEach(function(attr) {
      const v = hourMap[attr];
      if (v == null) return;
      const el = document.querySelector('[data-' + attr + ']');
      if (el) el.dataset.todayHours = String(v);
    });
    if (t.calls != null) {
      const el = document.querySelector('[data-snap-total-calls]');
      if (el) el.dataset.todayInt = String(t.calls);
    }
    // Today panel inline metrics
    const today = document.querySelector('.p-today');
    if (today) {
      const metrics = today.querySelectorAll('.today-metric .v');
      if (metrics.length >= 4) {
        if (t.calls != null) metrics[0].textContent = fmtIntRu(t.calls);
        if (t.cost_usd != null) metrics[1].dataset.usd = String(t.cost_usd);
        if (t.active_hours != null && metrics[2].childNodes.length > 0) {
          metrics[2].childNodes[0].nodeValue = Number(t.active_hours).toFixed(1);
        }
        if (t.profanity != null) metrics[3].textContent = String(t.profanity);
      }
      const topSession = today.querySelector('.today-session .v');
      if (topSession && t.top_session) topSession.textContent = t.top_session;
    }
  }

  function applyTimelineSummary(snap) {
    if (!snap.totals) return;
    const range = document.querySelector('[data-snap-tl-range]');
    if (range && snap.totals.period_start && snap.totals.period_end) {
      range.textContent = snap.totals.period_start + ' → ' + snap.totals.period_end;
    }
    if (Array.isArray(snap.timeline_weights) && snap.timeline_weights.length) {
      let peakIdx = 0;
      let peakVal = -1;
      snap.timeline_weights.forEach(function(v, i) {
        if (v > peakVal) { peakVal = v; peakIdx = i; }
      });
      const peakCallsEl = document.querySelector('[data-snap-tl-peak-calls]');
      const peakDateEl = document.querySelector('[data-snap-tl-peak-date]');
      if (peakCallsEl) peakCallsEl.textContent = fmtIntRu(peakVal);
      if (peakDateEl && snap.totals.period_start) {
        // peak date = period_start + peakIdx days
        try {
          const start = new Date(snap.totals.period_start + 'T00:00:00');
          if (Number.isFinite(start.getTime())) {
            const peakDate = new Date(start.getTime() + peakIdx * 86400000);
            peakDateEl.textContent = peakDate.toISOString().slice(0, 10);
          }
        } catch (e) { /* keep current */ }
      }
    }
  }

  function applySnapshot(snap) {
    if (!snap || typeof snap !== 'object') return;

    // 1. Pick up the new period length first — everything else uses it.
    if (snap.totals && snap.totals.days && Number.isFinite(snap.totals.days) && snap.totals.days > 0) {
      DAYS_TOTAL = snap.totals.days;
    }

    // 2. Override base/today data attributes that drive applyPeriod's pro-rate
    applyTotalsToBaseAttrs(snap);
    applyProviders(snap);
    applyProductivityToBaseAttrs(snap);
    applyHeroBadges(snap);
    applyTodayToTodayAttrs(snap);

    // 3. Refresh standalone scalar panels (budget, sentiment) directly
    setSnapBudget(snap);
    setSnapSentiment(snap);

    // 4. Re-render lists (models, sessions)
    rebuildModelsList(snap.models);
    rebuildSessionsList(snap.sessions);

    // 5. Timeline bars + summary range/peak
    if (Array.isArray(snap.timeline_weights) && snap.timeline_weights.length) {
      rebuildTimelineFromWeights(snap.timeline_weights, snap.timeline_days);
    }
    applyTimelineSummary(snap);
  }

  function loadSnapshot() {
    if (!SNAPSHOT_URL) {
      setSnapshotStatus('demo');
      return Promise.resolve(null);
    }
    return fetch(SNAPSHOT_URL, { cache: 'no-store' })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(snap) {
        if (!snap) {
          setSnapshotStatus('demo');
          return null;
        }
        SNAPSHOT_DATA = snap;
        applySnapshot(snap);
        // Status: live if generated within last 30 min, stale otherwise
        let hint = '';
        if (snap.generated_at) {
          const ts = new Date(snap.generated_at);
          const tsMs = ts.getTime();
          if (Number.isFinite(tsMs)) {
            const ageMs = Date.now() - tsMs;
            const hh = String(ts.getHours()).padStart(2, '0');
            const mm = String(ts.getMinutes()).padStart(2, '0');
            hint = hh + ':' + mm;
            // Future timestamps treated as stale (clock skew / parse error).
            const isLive = ageMs >= 0 && ageMs < 30 * 60 * 1000;
            setSnapshotStatus(isLive ? 'live' : 'stale', hint);
          } else {
            setSnapshotStatus('stale');
          }
        } else {
          setSnapshotStatus('stale');
        }
        return snap;
      })
      .catch(function() {
        setSnapshotStatus('demo');
        return null;
      });
  }

  function initMaPage() {
    applyMaTranslations();
    bindPeriodSelector();
    // Initial render uses PHP-baked values — instant paint.
    applyPeriod(getInitialPeriod());
    // Then snapshot (async) overrides + re-applies.
    loadSnapshot().then(function(snap) {
      if (snap) applyPeriod(getInitialPeriod());
    });
  }

  // Apply on load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMaPage);
  } else {
    initMaPage();
  }

  // Re-apply when global i18n switches lang — period labels + snapshot badge
  document.addEventListener('i18n:applied', function() {
    applyMaTranslations();
    applyPeriod(getInitialPeriod());
    renderSnapshotBadge();
  });
})();
</script>

<?php get_footer(); ?>
