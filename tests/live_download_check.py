"""Opt-in live download check; never logs team names or retains replay files."""
import argparse
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wows_replay_batch.core import Network, Tournament, TournamentClient, replay_meta


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--expect-archive', action='store_true')
    args = parser.parse_args()
    net = Network()
    tournament = Tournament.parse(args.url)
    client = TournamentClient(tournament, net)
    net.json('GET', 'https://wows-tournaments.com/api/identity/userinfo')
    token = net.session.cookies.get('wows-tournaments.antiforgery-request')
    listing = net.json('POST', 'https://wows-tournaments.com/api/matches/list',
        headers={**tournament.headers, 'X-XSRF-TOKEN': token},
        json={'signupId': None, 'stageId': None, 'groupId': None, 'roundId': None, 'team': '',
              'matchStatus': [], 'page': 1, 'pageSize': 21, 'sortColumn': None, 'sortDirection': 0})
    jobs = []
    for short in listing.get('items', []):
        match = client.load_match(short['id'])
        tag = match['matchOpponents'][0]['signup']['teamTag']
        jobs, _ = client.discover([match], tag, both=True)
        if jobs:
            break
    if not jobs:
        raise ValueError('No public replay was available for this check')
    with tempfile.TemporaryDirectory() as folder:
        for index, job in enumerate(jobs[:2], 1):
            target = Path(folder) / f'sample-{index}.wowsreplay'
            net.download(job['download_url'], target, headers=tournament.headers, replay_job=job, validator=replay_meta)
            metadata = replay_meta(target)
            print('VALID_REPLAY', index, target.stat().st_size, metadata['clientVersionFromExe'], flush=True)
    if args.expect_archive and not net.replay_archives:
        raise ValueError('The expected archive route was not exercised')
    print('LIVE_DOWNLOAD_CHECK_PASSED', 'archived' if net.replay_archives else 'direct', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Public CI logs must not include players, share tokens or storage paths.
        print('LIVE_DOWNLOAD_CHECK_FAILED', type(error).__name__, re.sub(r'https?://\S+', '<remote>', str(error)), flush=True)
        sys.exit(1)
