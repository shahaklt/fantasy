// Every stat the app shows: what it is, how to read it, and the trap.
// Written to be read in the two moments that matter — draft day, and Sunday.

export const CONCEPTS = [
  {
    h: 'Nothing here is a single number',
    p: `Every projection is the summary of thousands of simulated seasons, not a
        forecast of one. When a player shows 240 points, that is the average of
        what happened across those runs — he finished under 180 in some and over
        300 in others. The spread is the useful part, and it is why the floor,
        ceiling and boom rate columns exist. A projection with no distribution
        attached cannot tell you whether a player is a safe start or a swing.`,
  },
  {
    h: 'Points do not win leagues; points above replacement do',
    p: `The best quarterback usually outscores the best running back by a wide
        margin, and it almost never matters. You can only start one quarterback,
        and the twelfth-best one is close behind the best. What decides a draft
        is how far a player sits above the guy you could have had at that
        position instead. That is what <code>VORP</code> measures, and it is why
        the draft board sorts by it rather than by projected points.`,
  },
  {
    h: 'Your teammates are correlated, and so are your opponents',
    p: `The simulator plays whole games, not isolated players. One margin and one
        total are drawn per matchup, and every player's line is drawn inside it.
        So a quarterback's passing yards are literally the sum of his receivers'
        receiving yards, and when a game shoots out, everyone in it goes up
        together. This is why stacking a quarterback with his own receiver raises
        both your ceiling and your floor risk, and why the league simulator gives
        different playoff odds than adding up individual projections would.`,
  },
  {
    h: 'The market knows things the model does not',
    p: `A consensus board reflects beat reporters, camp news and injuries the box
        scores have not caught up to. Projections here are blended with the
        market's ordering <em>within each position</em>, weighted by how much
        usable history a player has. A rookie leans almost entirely on the
        market; an entrenched starter leans on his own tape. When the model and
        the market disagree sharply, that is information, not an error to
        correct away.`,
  },
];

export const DRAFT_PLAYBOOK = [
  { h: 'Set your league up first',
    p: 'Settings → teams, scoring, roster slots, draft slot. Replacement level, auction values and the recommender all derive from these. Getting them wrong quietly poisons every number downstream.' },
  { h: 'Sort by VORP, not by points',
    p: 'The board defaults to this. It already accounts for how many of each position your league starts.' },
  { h: 'Find the cliffs before you need them',
    p: 'Read the Positional Scarcity panel. It shows what you lose by waiting twelve more picks at each position. The position with the steepest drop is the one to take now.' },
  { h: 'Check who survives to your next pick',
    p: 'The @N column is the probability a player is still there at your next turn. Anyone above ~70% can usually wait. Anyone below ~30% is gone if you pass.' },
  { h: 'Mark every pick, not just yours',
    p: 'The board only knows what you tell it. Recording opponents’ picks is what keeps availability and scarcity honest.' },
  { h: 'Ask for a recommendation when you are on the clock',
    p: 'It simulates the rest of the draft a few hundred times per candidate and ranks by the roster each pick leads to — which is not the same as ranking by projected points.' },
  { h: 'Trust tiers over ranks',
    p: 'The gap between the last player in a tier and the first in the next is real value. The gap between two players inside one tier is mostly noise.' },
];

export const SEASON_PLAYBOOK = [
  { h: 'Rebuild after the news',
    p: 'Data refreshes at midnight and noon on its own. After a significant injury or a depth-chart change, hit Rebuild sims so usage gets redistributed to the players who inherit the work.' },
  { h: 'Set lineups on start probability, not projection',
    p: 'Start % is the share of simulations in which a player belongs in your optimal lineup. It already accounts for his floor, his ceiling and who else you roster.' },
  { h: 'Match the shape to the situation',
    p: 'Ahead on paper? Start floors. Need a blow-up week? Start boom rate. The mean projection is the same in both cases; the right player is not.' },
  { h: 'Read the game before the player',
    p: 'Open the matchup on the Games page. Team total drives everything — the fastest way to find a start is to find the game the model likes more than the market does.' },
  { h: 'Check playoff odds before you trade',
    p: 'Enter both rosters in My League and re-run. A trade that adds points but wrecks a starting slot often lowers title odds.' },
];

/** where: which screen shows it. read: how to act on it. eg: worked example. */
export const STATS = [
  // ------------------------------------------------------------ projections
  { key: 'proj_points', name: 'Projected points', group: 'Value', where: 'Projections · Draft board',
    body: 'Mean fantasy points across every simulated season, in your league’s scoring. Already includes games missed to injury, so it is a full-season expectation rather than a per-game rate multiplied by seventeen.',
    read: 'Compare within a position, never across. A quarterback outscoring a running back by 40 points tells you nothing on its own.',
    eg: 'A 240-point running back and a 285-point quarterback: the running back is usually the better pick, because his replacement is far worse.' },

  { key: 'proj_ppg', name: 'Points per game', group: 'Value', where: 'Projections',
    body: 'Projected points divided by projected games. Strips out availability so you can compare a player expected to miss time against one who is not.',
    read: 'Use it to spot players whose season total is suppressed by injury risk rather than by role.',
    eg: 'Two backs both project 200 points. One at 13.3 ppg over 15 games is the better player; the other at 11.8 over 17 is just more available.' },

  { key: 'proj_games', name: 'Projected games', group: 'Value', where: 'Projections',
    body: 'Expected games active. Injuries are simulated as a persistent two-state chain rather than weekly coin flips, so absences run in multi-week blocks the way real ones do.',
    read: 'Below about 14.5 signals real durability risk — either age at a punishing position or a history of missed time.',
    eg: 'Skill players who hold a real role average roughly 15.3 games; a projection of 13.5 is the model saying it expects a stretch on the sideline.' },

  { key: 'p5 / p95', name: 'Floor and ceiling', group: 'Value', where: 'Projections · Player detail',
    body: 'The 5th and 95th percentile season outcomes. Ninety percent of simulated seasons land between them.',
    read: 'Width is risk. A narrow band is a known quantity; a wide one is a swing that can win or sink your season.',
    eg: 'A 210-point projection spanning 150–280 is a very different asset from one spanning 190–235, even though the draft board shows the same mean.' },

  { key: 'proj_sd', name: 'Standard deviation', group: 'Value', where: 'Player detail',
    body: 'Spread of season outcomes around the mean, in points.',
    read: 'Load up on high-variance players when you need to catch the field, and on low-variance ones when you are protecting a lead.',
    eg: null },

  // ------------------------------------------------------------------ draft
  { key: 'vorp', name: 'Value over replacement', group: 'Draft', where: 'Draft board',
    body: 'Projected points minus what a freely available player at the same position would score. The replacement line is set from your league — teams multiplied by starters, plus the bench depth people realistically end up starting through byes and injuries.',
    read: 'This is the draft board’s default sort and the number to trust when comparing across positions.',
    eg: 'In a 12-team league starting one quarterback, the replacement quarterback is roughly QB18 — which is why elite quarterbacks carry far less VORP than their raw points suggest.' },

  { key: 'vols', name: 'Value over last starter', group: 'Draft', where: 'Draft board (sortable)',
    body: 'The same idea against a stricter baseline: the worst player at that position who still starts somewhere in your league, with no bench allowance.',
    read: 'A sharper scarcity signal than VORP for the early rounds, where you are competing for starters rather than depth.',
    eg: null },

  { key: 'auction_value', name: 'Auction value', group: 'Draft', where: 'Draft board',
    body: 'VORP converted into dollars. Total money in the room is teams times budget; every drafted player costs at least $1, and the surplus above that is split in proportion to value over replacement.',
    read: 'These are maximum sensible prices, not targets. Winning a player at his exact value gains you nothing over the field.',
    eg: 'In a 12-team $200 league the top player lands near $60. If bidding passes his number, the profit is gone — let someone else have it.' },

  { key: 'tier', name: 'Tier', group: 'Draft', where: 'Draft board · Projections',
    body: 'Positional groups found by locating real cliffs in value, rather than fixed-size buckets. A break is declared where the drop to the next player is large relative to the typical drop at that position.',
    read: 'The most actionable number on the board. Players inside a tier are near-interchangeable; the fall between tiers is what actually costs you.',
    eg: 'Two backs left in tier 3 and your next pick is fourteen slots away — that is the cliff you cannot afford to fall off. Take one now.' },

  { key: 'adp', name: 'ADP / consensus rank', group: 'Draft', where: 'Draft board',
    body: 'Where the market drafts a player, from FantasyPros expert consensus and, when reachable, real mock-draft data.',
    read: 'The gap between ADP and your board is your edge. A player the model likes far more than the room is one you can wait on.',
    eg: 'ADP 44 with a board rank of 22 means you can likely take him a round later than his value suggests, and spend this pick on someone who will not last.' },

  { key: 'adp_sd', name: 'ADP dispersion', group: 'Draft', where: 'Player detail',
    body: 'How much experts disagree about a player, in draft slots. Drives the availability model.',
    read: 'High dispersion means unpredictable — he could go two rounds early or fall two rounds. Do not count on him being there.',
    eg: null },

  { key: 'avail_at_N', name: 'Availability at pick N', group: 'Draft', where: 'Draft board (@N column)',
    body: 'Probability a player is still on the board at your next pick, from his consensus rank and the spread of expert opinion around it.',
    read: 'Above 70%, you can usually wait and take the scarcer position now. Below 30%, it is now or never.',
    eg: '@25 showing 72% on one receiver and 9% on another tells you exactly which of the two to take first.' },

  { key: 'roster_value', name: 'Roster value', group: 'Draft', where: 'Recommend my pick',
    body: 'For each candidate, the app forces that pick, then simulates the rest of the draft hundreds of times — opponents reaching, falling and filling needs — and scores the best legal starting lineup the resulting roster produces.',
    read: 'This is the only number that answers "what should I actually do", because it accounts for who will still be there later.',
    eg: null },

  { key: 'value_vs_best', name: 'Value vs best', group: 'Draft', where: 'Recommend my pick',
    body: 'Points of finished-roster strength given up by taking this candidate instead of the top one.',
    read: 'Anything inside a few points is a coin flip — take the player you prefer. A gap of 40+ is the simulator telling you the alternative is a mistake.',
    eg: null },

  { key: 'drop_12', name: 'Positional drop-off', group: 'Draft', where: 'Positional scarcity',
    body: 'Points lost by waiting twelve more picks at each position instead of taking the best one available now. Also shown at one and five picks.',
    read: 'Rank positions by this, not by who is best overall. The steepest cliff is where your pick buys the most.',
    eg: 'Tight end showing a 107-point drop while receiver shows 79 means the tight end room is about to empty out — that is where the pick belongs.' },

  { key: 'role_confidence', name: 'Role confidence', group: 'Draft', where: 'Player detail',
    body: 'How much usable history the model has for this player, from 0 to 1. Drives the blend between his own tape and the market’s view, and widens the season-level uncertainty on players with low scores.',
    read: 'Low confidence means the projection is mostly the market’s opinion. Treat the range as wide and the mean as soft.',
    eg: 'A rookie sits near 0. A four-year starter sits near 0.75 and his projection is largely his own production.' },

  // ------------------------------------------------------------------ usage
  { key: 'target_share_proj', name: 'Target share', group: 'Usage', where: 'Player detail',
    body: 'Share of his team’s pass attempts projected to come his way. The single most stable and predictive input in the whole model.',
    read: 'Volume beats efficiency. A 25% target share on a mediocre offence usually beats 15% on a great one.',
    eg: 'League-wide, a team’s leading receiver takes about 23.7% of targets; anything above 28% is a genuine alpha role.' },

  { key: 'carry_share_proj', name: 'Carry share', group: 'Usage', where: 'Player detail',
    body: 'Share of team rushing attempts. Quarterback carries are modelled separately, since designed runs take roughly 15% of a team’s carries league-wide.',
    read: 'Backfields are winner-take-most. Above 50% is a true lead back; 30–45% is a committee and the outcome depends on the split holding.',
    eg: null },

  { key: 'dropback_share_proj', name: 'Dropback share', group: 'Usage', where: 'Player detail',
    body: 'Share of team pass attempts for a quarterback. Derived from who actually starts — depth chart and market rank — rather than from season averages.',
    read: 'Near 0.93 is a clear starter. Anything materially lower means the job is genuinely contested.',
    eg: 'Season-average history would show a starter who missed four games at 0.57 and invent a timeshare that will not happen — which is why this is re-derived.' },

  { key: 'yards_per_target', name: 'Efficiency rates', group: 'Usage', where: 'Player detail',
    body: 'Yards per target, catch rate and yards per carry, each shrunk toward the positional average with an explicit prior — 260 attempts for yards per attempt, 55 targets for catch rate, and so on.',
    read: 'Efficiency is far less stable year to year than volume. Treat a big number here as much softer evidence than a big target share.',
    eg: null },

  // --------------------------------------------------------------- weekly
  { key: 'floor / ceiling', name: 'Weekly floor and ceiling', group: 'Weekly', where: 'Games · Weekly · Start/sit',
    body: '10th and 90th percentile outcomes for a single week.',
    read: 'Floor when you are favoured and protecting a lead; ceiling when you are behind and need the top end.',
    eg: null },

  { key: 'boom_pct', name: 'Boom rate', group: 'Weekly', where: 'Stars · Weekly',
    body: 'Share of simulations in which a player scores 20 or more.',
    read: 'The number that finds tournament plays and desperation starts. Ranks differently from the mean, on purpose.',
    eg: 'A 14-point projection with a 44% boom rate beats a 15-point projection with a 22% boom rate in any week you have to win.' },

  { key: 'bust_pct', name: 'Bust rate', group: 'Weekly', where: 'Weekly',
    body: 'Share of simulations under 5 points.',
    read: 'The risk you are actually carrying by starting someone. Above 25% for a supposed starter is a warning.',
    eg: null },

  { key: 'active_pct', name: 'Active rate', group: 'Weekly', where: 'Weekly',
    body: 'Share of simulations in which the player suits up that week.',
    read: 'Below 90% means the injury model has real doubt. Have a contingency ready.',
    eg: null },

  { key: 'start_pct', name: 'Start percentage', group: 'Weekly', where: 'My League · Start/sit',
    body: 'Share of simulations in which this player belongs in your optimal starting lineup, given everyone else you roster.',
    read: 'The single best lineup number, because it already weighs floor, ceiling and your alternatives against each other.',
    eg: 'Anything above 60% starts. Between 25% and 60% is a genuine decision. Under 25% sits.' },

  // ---------------------------------------------------------------- league
  { key: 'playoff_odds', name: 'Playoff and title odds', group: 'League', where: 'My League',
    body: 'Share of simulated seasons in which your roster makes the playoffs, and wins it all. Every team is evaluated against the same simulated NFL seasons, so shared players are correlated the way they really are.',
    read: 'The right way to judge a trade. Points can go up while title odds go down, if the trade breaks a starting slot.',
    eg: null },

  { key: 'proj_wins', name: 'Projected wins', group: 'League', where: 'My League',
    body: 'Mean regular-season wins across simulations, using head-to-head weekly scores rather than season totals.',
    read: 'Compare against points scored. A high-points, low-wins team is unlucky, not bad — and usually a buy.',
    eg: null },

  // ----------------------------------------------------------------- games
  { key: 'proj_total', name: 'Projected total and margin', group: 'Games', where: 'Games',
    body: 'Mean simulated combined score and winning margin. Each game is drawn as one coherent contest anchored on the closing line, so both sides are consistent with each other.',
    read: 'Team total is the strongest driver of fantasy scoring there is. Find the games the model likes and start the players in them.',
    eg: null },

  { key: 'spread_edge', name: 'Spread and total edge', group: 'Games', where: 'Games',
    body: 'Difference between the model’s mean margin or total and the market’s posted line.',
    read: 'Anything inside about 1.5 points is noise — the simulation is anchored to that line. Larger gaps come from the model’s own view of team strength.',
    eg: null },

  { key: 'home_cover_prob', name: 'Win, cover and over probability', group: 'Games', where: 'Games · Game detail',
    body: 'Share of simulations in which a team wins outright, covers the posted spread, or the game clears the total. Alternate lines are priced off the same simulated distribution.',
    read: 'Cover probability sitting near 50% is the expected result and confirms the sim is anchored correctly. Departures from it are the model disagreeing.',
    eg: null },

  { key: 'margin_sd', name: 'Margin and total spread', group: 'Games', where: 'Game detail',
    body: 'How wide the simulated outcomes are. Historically the final margin lands about 12.7 points either side of the closing spread, and the total about 13.2 either side of its line.',
    read: 'A useful reality check on any single-game confidence: NFL games are far less predictable than the point estimate suggests.',
    eg: null },

  { key: 'implied_volatility', name: 'Implied volatility', group: 'Games', where: 'Game detail',
    body: 'The margin spread that the market’s own moneyline and spread jointly imply — the sports analogue of an option’s implied vol.',
    read: 'Well above the 12.7 baseline means the market is pricing unusual uncertainty: a backup quarterback, bad weather, or a team with nothing to play for.',
    eg: null },

  // --------------------------------------------------------------- markets
  { key: 'model_prob', name: 'Model, market and blended probability', group: 'Markets', where: 'Markets',
    body: 'What the simulation thinks, what the contract costs, and the two combined in log-odds space with a capped shift.',
    read: 'Trade the blended number, not the raw model. The cap exists so one wrong assumption cannot produce an enormous position against a market that knows something you do not.',
    eg: null },

  { key: 'edge', name: 'Edge', group: 'Markets', where: 'Markets',
    body: 'Blended probability minus the price you would pay.',
    read: 'Small edges on liquid markets are the only real ones. A 20-point edge almost always means the contract was matched to the wrong question — check what it actually settles on.',
    eg: null },

  { key: 'kelly_fraction', name: 'Kelly fraction and stake', group: 'Markets', where: 'Markets',
    body: 'Fraction of bankroll that maximises long-run growth, scaled down, capped, and shrunk further by the uncertainty in the model’s own probability.',
    read: 'Full Kelly is far too aggressive when the edge itself is an estimate. Overbetting is punished much harder than underbetting.',
    eg: 'A 4-point edge with a 6-point standard error is shrunk to nearly nothing on purpose — it is indistinguishable from noise.' },

  { key: 'micro_price', name: 'Micro-price', group: 'Live tape', where: 'Live Tape',
    body: 'The mid-price adjusted for how much size rests on each side of the book. A martingale by construction, and a better predictor of the next price than the mid itself.',
    read: 'Micro-price pulling away from the mid is the book leaning before the last trade moves.',
    eg: null },

  { key: 'imbalance', name: 'Book imbalance and order flow', group: 'Live tape', where: 'Live Tape',
    body: 'Depth on the bid minus the ask as a share of the total, and the signed change in depth at the touch between snapshots.',
    read: 'Where sentiment shows up first. Persistent one-sided flow moves price before the printed price reflects it.',
    eg: null },

  { key: 'momentum', name: 'Momentum and realised volatility', group: 'Live tape', where: 'Live Tape',
    body: 'Recent drift normalised by its own volatility, and the standard deviation of successive price changes.',
    read: 'Momentum is a z-score, not a return — above roughly 2 means the move is large relative to this contract’s own noise.',
    eg: null },
];

export const GROUPS = ['Value', 'Draft', 'Usage', 'Weekly', 'League', 'Games', 'Markets', 'Live tape'];
