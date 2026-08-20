"""Builders for the scraper payload shapes the tests exercise."""


def scrape(status='Playing', minute='0', fs=('0', '0'), hts=None, ps=None,
           events=None):
    """
    A minimal get_match_data() return value.

    Only the fields the phase and early-posting logic actually read are set;
    everything else in a real scrape is irrelevant to those decisions.

      status  'Fixture' | 'Playing' | 'Played'
      minute  the scraper's clock, as it reports it (a string, '90+3' allowed)
      fs      (home, away) live score
      hts     (home, away) half-time score — only filled in at the whistle
      ps      (home, away) penalty shootout score
      events  timeline entries: {'type', 'team', 'player', 'minute'}
    """
    ms = {'minute': minute, 'fs_A': fs[0], 'fs_B': fs[1]}
    if hts:
        ms['hts_A'], ms['hts_B'] = hts
    if ps:
        ms['ps_A'], ms['ps_B'] = ps
    return {'status': status, 'matchSample': ms, 'events': events or []}


def goal(team='A', player='Someone', minute='23', type='goal'):
    return {'type': type, 'team': team, 'player': player, 'minute': minute}
