import { useEffect, useMemo, useState } from 'react'

type SportLabel = 'ALL' | 'NFL' | 'NCAAF'
type SortDirection = 'asc' | 'desc'
type SortKey = 'edge' | 'game' | 'date' | 'line' | 'gap' | 'rating' | 'weather' | 'odds' | 'move'
const REFRESH_MS = 5 * 60 * 1000

type BoardRow = {
  game_id: string
  data_source?: 'sportsbook' | 'kalshi'
  sport: 'NFL' | 'NCAAF'
  bet_type?: 'spread' | 'total' | 'moneyline'
  edge_score?: number
  commence_time: string
  away_team: string
  home_team: string
  market: {
    consensus_spread: number | null
    best_favorite_line: number | null
    best_underdog_line: number | null
    opening_spread: number | null
    book_count: number
    latest_book: string | null
    latest_timestamp: string | null
  }
  model: {
    fair_spread: number | null
    source: string | null
    updated_at: string | null
  }
  contract?: {
    ticker: string
    title: string
    side_label: string | null
    yes_bid: number | null
    yes_ask: number | null
    no_bid: number | null
    no_ask: number | null
    last_price: number | null
    previous_price: number | null
    price_move: number | null
    volume: number
    volume_24h: number
    open_interest: number
    status: string | null
  }
  bluechip?: {
    market_line: string | null
    model_line: string | null
    market_team: string | null
    model_team: string | null
    market_spread: number | null
    model_spread: number | null
    gap: number | null
    edge_team: string | null
    book_spread: string | null
    book_total: number | null
    odds_implied_score: string | null
    betting_source: string | null
    summary: string | null
    weather: {
      venue: string | null
      condition: string | null
      temperature_f: number | null
      wind_mph: number | null
      source: string
      map_url: string | null
    }
    source: string
    url: string
    updated_at: string | null
  } | null
  projection?: {
    model_winner: string | null
    model_margin: number | null
    model_label: string | null
    model_line: string | null
    market_line: string | null
    edge_team: string | null
    spread_edge: number | null
    book_total: number | null
    projected_total: number | null
    total_edge: number | null
    total_lean: string | null
    projected_score: {
      away_team: string
      away_points: number
      home_team: string
      home_points: number
      label: string
    } | null
  } | null
  weather_impact?: {
    score: number
    category: string
    wind_impact: number
    precipitation_impact: number
    temperature_impact: number
    spread_adjustment: number
    total_adjustment: number
    adjusted_total: number | null
    adjusted_spread: number | null
    projected_score: {
      team_a_points: number
      team_b_points: number
    } | null
    confidence: number
    assumptions: {
      rain_pct: number
      snow_in: number
      gust_mph: number
    }
  } | null
  metrics: {
    model_market_gap: number | null
    line_move: number | null
    confidence_score: number
  }
  rating?: {
    probability: number | null
    grade: 'Excellent' | 'Great' | 'Good' | 'Even'
    edge: number | null
    summary: string
    reasons: string[]
  }
  updated_at: string
}

type BoardResponse = {
  generated_at: string
  source: 'sportsbook' | 'kalshi' | 'live' | 'preview'
  status: string
  rows: BoardRow[]
}

const emptyBoard: BoardResponse = {
  generated_at: new Date().toISOString(),
  source: 'preview',
  status: 'Loading market board...',
  rows: [],
}

function formatDate(value: string | null) {
  if (!value) return '-'
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

function formatNumber(value: number | null, digits = 1) {
  return value === null || !Number.isFinite(value) ? '-' : value.toFixed(digits)
}

function formatCents(value: number | null) {
  return value === null || !Number.isFinite(value) ? '-' : `${Math.round(value * 100)}c`
}

function formatSigned(value: number | null, digits = 1) {
  if (value === null || !Number.isFinite(value)) return '-'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`
}

function coverOdds(row: BoardRow) {
  return row.contract?.last_price ?? row.contract?.yes_bid ?? row.contract?.yes_ask ?? null
}

function modelGap(row: BoardRow) {
  return row.bluechip?.gap ?? row.metrics.model_market_gap ?? null
}

function spreadEdge(row: BoardRow) {
  return row.projection?.spread_edge ?? modelGap(row)
}

function modelOpinion(row: BoardRow) {
  return row.projection?.model_label ?? row.projection?.model_line ?? row.bluechip?.model_line ?? 'Model unavailable'
}

function marketSpread(row: BoardRow) {
  return row.projection?.market_line ?? row.bluechip?.market_line ?? consensusLabel(row)
}

function spreadDecision(row: BoardRow) {
  const edgeTeam = row.projection?.edge_team
  const edge = spreadEdge(row)
  if (edgeTeam && edge !== null && Number.isFinite(edge)) return `${edgeTeam} +${edge.toFixed(1)} vs spread`
  if (edge !== null && Number.isFinite(edge)) return `${formatSigned(edge)} pts vs spread`
  return 'Spread edge unavailable'
}

function projectedTotal(row: BoardRow) {
  const projection = row.projection
  if (projection?.projected_total === null || projection?.projected_total === undefined) return 'Projected total unavailable'
  const market = projection.book_total ? ` vs ${formatNumber(projection.book_total)}` : ''
  const lean = projection.total_lean && projection.total_lean !== 'No clear total edge' ? ` ${projection.total_lean}` : ''
  return `${formatNumber(projection.projected_total)}${market}${lean}`
}

function projectedScore(row: BoardRow) {
  return row.projection?.projected_score?.label ?? row.bluechip?.odds_implied_score ?? '-'
}

function formatWeather(row: BoardRow) {
  const weather = row.bluechip?.weather
  if (!weather?.condition) return '-'
  const temp = weather.temperature_f === null ? '' : `, ${formatNumber(weather.temperature_f, 0)}F`
  const wind = weather.wind_mph === null ? '' : `, ${formatNumber(weather.wind_mph, 0)} mph`
  return `${weather.condition}${temp}${wind}`
}

function formatWeatherImpact(row: BoardRow) {
  const impact = row.weather_impact
  if (!impact || Math.abs(impact.total_adjustment) < 0.1) return null
  const total = impact.adjusted_total === null ? 'total lean' : `adjusted total ${formatNumber(impact.adjusted_total)}`
  return `Weather ${total} (${formatSigned(impact.total_adjustment)} pts)`
}

function weatherColumnLabel(row: BoardRow) {
  const impact = row.weather_impact
  if (!impact) return '-'
  const adjustment = impact.total_adjustment
  const condition = row.bluechip?.weather?.condition ?? impact.category
  if (Math.abs(adjustment) < 0.1) {
    if (condition.toLowerCase().includes('sun') || condition.toLowerCase().includes('clear')) return 'Clear'
    return impact.score > 0 ? impact.category : 'Calm'
  }
  const conditionText = condition.toLowerCase()
  const tag = conditionText.includes('rain') || conditionText.includes('shower')
    ? 'Rain'
    : conditionText.includes('snow')
      ? 'Snow'
      : (row.bluechip?.weather?.wind_mph ?? 0) >= 12
        ? 'Wind'
        : 'Weather'
  return `${tag} ${formatSigned(adjustment)}`
}

function weatherSortValue(row: BoardRow) {
  return Math.abs(row.weather_impact?.total_adjustment ?? 0)
}

function normalizeTeam(value: string) {
  return value
    .toLowerCase()
    .replace(/\bst[.]?\b/g, 'state')
    .replace(/&/g, ' and ')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
}

function teamTokens(value: string) {
  return normalizeTeam(value).split(' ').filter(Boolean)
}

function marketGroupKey(row: BoardRow) {
  const teams = [row.away_team, row.home_team]
    .map(normalizeTeam)
    .sort()
    .join('|')
  return `${row.sport}|${teams}|${row.bet_type ?? 'spread'}`
}

function rowMatchesSearch(row: BoardRow, rawQuery: string, exactTeamKeys: Set<string>) {
  const query = normalizeTeam(rawQuery)
  if (!query) return true
  const away = normalizeTeam(row.away_team)
  const home = normalizeTeam(row.home_team)
  if (exactTeamKeys.has(query)) return away === query || home === query

  const queryTokens = teamTokens(query)
  const matchupText = `${away} ${home}`
  return queryTokens.every((token) => matchupText.includes(token))
}

function bestMarketRow(current: BoardRow | undefined, candidate: BoardRow) {
  if (!current) return candidate
  const currentScore = current.edge_score ?? 0
  const candidateScore = candidate.edge_score ?? 0
  if (candidateScore !== currentScore) return candidateScore > currentScore ? candidate : current
  const currentProbability = current.rating?.probability ?? 0
  const candidateProbability = candidate.rating?.probability ?? 0
  if (candidateProbability !== currentProbability) return candidateProbability > currentProbability ? candidate : current
  return Math.abs(candidate.metrics.line_move ?? 0) > Math.abs(current.metrics.line_move ?? 0) ? candidate : current
}

function marketTypeLabel(row: BoardRow) {
  if (row.bet_type === 'moneyline') return 'Moneyline'
  return row.bet_type === 'total' ? 'Over/under' : 'Spread'
}

function ratingGrade(row: BoardRow) {
  return row.rating?.grade ?? 'Even'
}

function ratingClass(row: BoardRow) {
  return ratingGrade(row).toLowerCase()
}

function ratingPercent(row: BoardRow) {
  const probability = row.rating?.probability
  return probability === null || probability === undefined || !Number.isFinite(probability)
    ? '-'
    : `${probability.toFixed(1)}%`
}

function consensusLabel(row: BoardRow) {
  if (row.data_source === 'kalshi' && row.contract) {
    const strike = row.market.consensus_spread
    const spread = strike === null ? '' : ` > ${formatNumber(strike)}`
    return `${row.contract.side_label ?? row.contract.title}${spread}`
  }
  const spread = row.market.consensus_spread
  if (spread === null) return '-'
  if (spread < 0) return `${row.home_team} ${spread.toFixed(1)}`
  if (spread > 0) return `${row.away_team} ${(-spread).toFixed(1)}`
  return 'Pick'
}

function lineValue(row: BoardRow) {
  return row.market.consensus_spread
}

function dateValue(value: string | null) {
  if (!value) return null
  const time = new Date(value).getTime()
  return Number.isFinite(time) ? time : null
}

function compareText(a: string, b: string, direction: SortDirection) {
  const result = a.localeCompare(b, undefined, { sensitivity: 'base' })
  return direction === 'asc' ? result : -result
}

function compareNumber(a: number | null | undefined, b: number | null | undefined, direction: SortDirection) {
  const aValid = a !== null && a !== undefined && Number.isFinite(a)
  const bValid = b !== null && b !== undefined && Number.isFinite(b)
  if (!aValid && !bValid) return 0
  if (!aValid) return 1
  if (!bValid) return -1
  const result = a - b
  return direction === 'asc' ? result : -result
}

async function fetchBoard(): Promise<BoardResponse> {
  try {
    const response = await fetch('/api/board', { cache: 'no-store' })
    if (!response.ok) throw new Error('API unavailable')
    return (await response.json()) as BoardResponse
  } catch {
    const response = await fetch('/data/board-preview.json', { cache: 'no-store' })
    if (!response.ok) return emptyBoard
    return { ...((await response.json()) as BoardResponse), source: 'preview' }
  }
}

function App() {
  const [board, setBoard] = useState<BoardResponse>(emptyBoard)
  const [selectedSport, setSelectedSport] = useState<SportLabel>('NCAAF')
  const [sortKey, setSortKey] = useState<SortKey>('edge')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [selectedGameId, setSelectedGameId] = useState('')
  const [expandedRatingId, setExpandedRatingId] = useState('')
  const [teamSearch, setTeamSearch] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    let active = true

    async function refresh(showRefreshing = false) {
      if (showRefreshing) setRefreshing(true)
      const nextBoard = await fetchBoard()
      if (!active) return
      setBoard(nextBoard)
      if (showRefreshing) setRefreshing(false)
    }

    void refresh(true)
    const timer = window.setInterval(() => {
      void refresh()
    }, REFRESH_MS)

    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

  const rows = useMemo(() => {
    const sportRows =
      selectedSport === 'ALL' ? board.rows : board.rows.filter((row) => row.sport === selectedSport)
    const exactTeamKeys = new Set<string>()
    for (const row of sportRows) {
      exactTeamKeys.add(normalizeTeam(row.away_team))
      exactTeamKeys.add(normalizeTeam(row.home_team))
    }
    const query = teamSearch.trim()
    const searchedRows = query
      ? sportRows.filter((row) => rowMatchesSearch(row, query, exactTeamKeys))
      : sportRows
    const grouped = new Map<string, BoardRow>()
    for (const row of searchedRows) {
      const key = marketGroupKey(row)
      grouped.set(key, bestMarketRow(grouped.get(key), row))
    }
    return [...grouped.values()].sort((a, b) => {
      let result = 0
      if (sortKey === 'game') {
        result = compareText(`${a.away_team} ${a.home_team}`, `${b.away_team} ${b.home_team}`, sortDirection)
      } else if (sortKey === 'date') {
        result = compareNumber(dateValue(a.commence_time), dateValue(b.commence_time), sortDirection)
      } else if (sortKey === 'line') {
        result = compareNumber(lineValue(a), lineValue(b), sortDirection)
      } else if (sortKey === 'gap') {
        result = compareNumber(spreadEdge(a), spreadEdge(b), sortDirection)
      } else if (sortKey === 'rating') {
        result = compareNumber(a.rating?.probability, b.rating?.probability, sortDirection)
      } else if (sortKey === 'weather') {
        result = compareNumber(weatherSortValue(a), weatherSortValue(b), sortDirection)
      } else if (sortKey === 'odds') {
        result = compareNumber(coverOdds(a), coverOdds(b), sortDirection)
      } else if (sortKey === 'move') {
        result = compareNumber(a.metrics.line_move, b.metrics.line_move, sortDirection)
      } else {
        result = compareNumber(a.edge_score, b.edge_score, sortDirection)
      }
      return result || compareNumber(a.edge_score, b.edge_score, 'desc')
    })
  }, [board.rows, selectedSport, sortDirection, sortKey, teamSearch])

  const selectedRow = rows.find((row) => row.game_id === selectedGameId) ?? rows[0] ?? null
  const topGap = rows.reduce<number | null>((largest, row) => {
    const gap = spreadEdge(row)
    if (gap === null || !Number.isFinite(gap) || gap <= 0) return largest
    return largest === null || gap > largest ? gap : largest
  }, null)
  const latestUpdate = rows.reduce<string | null>((latest, row) => {
    if (!latest) return row.updated_at
    return new Date(row.updated_at) > new Date(latest) ? row.updated_at : latest
  }, null)
  const modelSource =
    selectedSport === 'NCAAF' ? 'Blue Chip model' : selectedSport === 'NFL' ? 'Model pending' : 'Models where available'
  const oddsSource = board.source === 'preview' ? 'Preview odds' : board.source === 'kalshi' ? 'Kalshi odds' : 'Sportsbook odds'

  function toggleSort(nextKey: SortKey) {
    if (nextKey === sortKey) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc')
      return
    }
    setSortKey(nextKey)
    setSortDirection(nextKey === 'game' || nextKey === 'date' || nextKey === 'line' ? 'asc' : 'desc')
  }

  function sortLabel(key: SortKey) {
    if (key !== sortKey) return ''
    return sortDirection === 'asc' ? ' asc' : ' desc'
  }

  return (
    <main className="shell">
      <aside className="sidebar" aria-label="Controls">
        <div className="brand">
          <span className="brand-mark">MB</span>
          <div>
            <p>MarkBets</p>
            <h1>Coverage Desk</h1>
          </div>
        </div>

        <label className="field">
          <span>Team search</span>
          <input
            onChange={(event) => setTeamSearch(event.target.value)}
            placeholder="BYU, Texas, Notre Dame"
            type="search"
            value={teamSearch}
          />
        </label>

        <div className="field">
          <span>Sport</span>
          <div className="segmented three">
            {(['ALL', 'NFL', 'NCAAF'] as SportLabel[]).map((sport) => (
              <button
                className={selectedSport === sport ? 'selected' : ''}
                key={sport}
                onClick={() => setSelectedSport(sport)}
                type="button"
              >
                {sport}
              </button>
            ))}
          </div>
        </div>

        <div className="feed-note">
          <span>{refreshing ? 'Updating now' : 'Auto-updates every 5 min'}</span>
          <small>{rows.length.toLocaleString()} ranked lines</small>
        </div>
      </aside>

      <section className="content">
        <div className="topbar">
          <div>
            <p className="eyebrow">{selectedSport === 'ALL' ? 'All football' : selectedSport} board</p>
            <h2>Best edges</h2>
            <p className="board-meta">
              {rows.length.toLocaleString()} lines · Top gap {formatSigned(topGap)} · {modelSource} · {oddsSource}
              {latestUpdate ? ` · Updated ${formatDate(latestUpdate)}` : ''}
            </p>
          </div>
        </div>

        <section className="detail-grid">
          {selectedRow ? (
            <>
              <div className="detail-main">
                <div className="ticket-head">
                  <div>
                    <p className="eyebrow">{selectedRow.sport} / {marketTypeLabel(selectedRow)}</p>
                    <h3>{selectedRow.away_team} vs {selectedRow.home_team}</h3>
                    <p>Model says {modelOpinion(selectedRow)}. {spreadDecision(selectedRow)}.</p>
                  </div>
                  <div className={`ticket-rating ${ratingClass(selectedRow)}`}>
                    <span>{ratingGrade(selectedRow)}</span>
                    <strong>{ratingPercent(selectedRow)}</strong>
                  </div>
                </div>

                <div className="ticket-grid" aria-label="Selected market details">
                  <div>
                    <span>Model</span>
                    <strong>{modelOpinion(selectedRow)}</strong>
                  </div>
                  <div>
                    <span>Market spread</span>
                    <strong>{marketSpread(selectedRow)}</strong>
                  </div>
                  <div>
                    <span>Cover edge</span>
                    <strong>{spreadDecision(selectedRow)}</strong>
                  </div>
                  <div>
                    <span>Projected total</span>
                    <strong>{projectedTotal(selectedRow)}</strong>
                  </div>
                  <div>
                    <span>Projected score</span>
                    <strong>{projectedScore(selectedRow)}</strong>
                  </div>
                  <div>
                    <span>Odds</span>
                    <strong>{formatCents(coverOdds(selectedRow))}</strong>
                  </div>
                </div>
              </div>
              <div className="detail-stack">
                <div>
                  <span>Why this rating</span>
                  <strong>{selectedRow.rating?.summary ?? 'Rating explanation unavailable'}</strong>
                  {(selectedRow.rating?.reasons ?? []).slice(0, 3).map((reason) => (
                    <small key={reason}>{reason}</small>
                  ))}
                </div>
                {formatWeatherImpact(selectedRow) ? (
                  <div>
                    <span>Weather</span>
                    <strong>{formatSigned(selectedRow.weather_impact?.total_adjustment ?? null)} pts</strong>
                    <small>{formatWeather(selectedRow)}</small>
                  </div>
                ) : null}
              </div>
            </>
          ) : (
            <div className="empty-panel">No market selected yet.</div>
          )}
        </section>

        <section className="board-panel">
          <div className="panel-heading">
            <div>
              <h3>Ranked line breakdown</h3>
              <p>Select a row to update the market breakdown above.</p>
            </div>
          </div>

          <div className="breakdown-header">
            <button className={sortKey === 'edge' ? 'active' : ''} onClick={() => toggleSort('edge')} type="button">
              #<span>{sortLabel('edge')}</span>
            </button>
            <button className={sortKey === 'game' ? 'active' : ''} onClick={() => toggleSort('game')} type="button">
              Game<span>{sortLabel('game')}</span>
            </button>
            <button className={sortKey === 'date' ? 'active' : ''} onClick={() => toggleSort('date')} type="button">
              Date<span>{sortLabel('date')}</span>
            </button>
            <button className={sortKey === 'line' ? 'active' : ''} onClick={() => toggleSort('line')} type="button">
              Model<span>{sortLabel('line')}</span>
            </button>
            <button className={sortKey === 'gap' ? 'active' : ''} onClick={() => toggleSort('gap')} type="button">
              Gap<span>{sortLabel('gap')}</span>
            </button>
            <button className={sortKey === 'rating' ? 'active' : ''} onClick={() => toggleSort('rating')} type="button">
              Rating<span>{sortLabel('rating')}</span>
            </button>
            <button className={sortKey === 'weather' ? 'active' : ''} onClick={() => toggleSort('weather')} type="button">
              Weather<span>{sortLabel('weather')}</span>
            </button>
            <button className={sortKey === 'odds' ? 'active' : ''} onClick={() => toggleSort('odds')} type="button">
              Odds<span>{sortLabel('odds')}</span>
            </button>
          </div>

          <div className="breakdown-list">
            {rows.length ? (
              rows.slice(0, 200).map((row, index) => {
                const expanded = expandedRatingId === row.game_id
                return (
                  <div className={`breakdown-item ${expanded ? 'expanded' : ''}`} key={row.game_id}>
                    <div
                      className={`breakdown-row ${selectedRow?.game_id === row.game_id ? 'selected' : ''}`}
                      onClick={() => setSelectedGameId(row.game_id)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') setSelectedGameId(row.game_id)
                      }}
                      role="button"
                      tabIndex={0}
                    >
                      <span className="rank">{index + 1}</span>
                      <span className="row-game">
                        <strong>
                          {row.away_team} vs {row.home_team}
                        </strong>
                        <small>
                          {row.sport} / {marketTypeLabel(row)}
                        </small>
                      </span>
                      <span className="row-date">
                        <span className="mobile-label">Date</span>
                        <strong>{formatDate(row.commence_time)}</strong>
                        <small>{row.sport}</small>
                      </span>
                      <span className="row-market">
                        <span className="mobile-label">Model</span>
                        <strong>{modelOpinion(row)}</strong>
                        <small>{marketSpread(row)} / {spreadDecision(row)}</small>
                      </span>
                      <span className="row-gap">
                        <span className="mobile-label">Gap</span>
                        <strong>{formatSigned(spreadEdge(row))}</strong>
                      </span>
                      <span className="row-rating">
                        <span className="mobile-label">Rating</span>
                        <button
                          className={`rating-pill ${ratingClass(row)}`}
                          onClick={(event) => {
                            event.stopPropagation()
                            setSelectedGameId(row.game_id)
                            setExpandedRatingId(expanded ? '' : row.game_id)
                          }}
                          type="button"
                        >
                          <strong>{ratingGrade(row)}</strong>
                          <span>{ratingPercent(row)}</span>
                        </button>
                      </span>
                      <span className="row-weather">
                        <span className="mobile-label">Weather</span>
                        <strong>{weatherColumnLabel(row)}</strong>
                        <small>{row.bluechip?.weather?.condition ?? 'No weather'}</small>
                      </span>
                      <span className="row-odds">
                        <span className="mobile-label">Odds</span>
                        <strong>{formatCents(coverOdds(row))}</strong>
                        <small>Total {projectedTotal(row)}</small>
                      </span>
                    </div>
                    {expanded ? (
                      <div className="rating-explanation">
                        <strong>{row.rating?.summary ?? 'Rating explanation unavailable'}</strong>
                        {(row.rating?.reasons ?? []).map((reason) => (
                          <p key={reason}>{reason}</p>
                        ))}
                      </div>
                    ) : null}
                  </div>
                )
              })
            ) : (
              <div className="empty">No live markets match that search.</div>
            )}
          </div>
        </section>

      </section>
    </main>
  )
}

export default App
