"""
Reddit structure:
    subreddit (r/learnpython)
        - submission
            - comment
                - comment
                    - comment
                - comment
                - comment
                        - ...

-
Backstory and context:

PRAW :
    - no functionality to get all data (limits to 1000 or something)
    - no functionality to query by time (! this is serious)
    + always up to date data

Solution to these limits is pushshift.io
https://pypi.org/project/pushshift.py/
https://pypi.org/project/pushshift.py/

PushshiftAPI limits:
    - looks like recent comments are sometimes not available on PushshiftAPI
    - the api gets delayed at times (ranging from a few hours to days).
        Check the following for created_utc
        https://api.pushshift.io/reddit/search/comment/?subreddit=askreddit
        More on this issue:
        https://www.reddit.com/r/pushshift/comments/fg3arm/observing_high_ingestion_delays_30_min_for_recent/
        https://www.reddit.com/r/pushshift/comments/gqzrky/delay_in_getting_comments/
    + can search by time

Solution to these limits is a combination of PRAW and PushshiftAPI:
    get submission ids on PushshiftAPI (filtered by time)
    for each id get data (submission and comments) on PRAW

Hard to tell if this is a good solution.
Querying twice (and more) instead of once is not optimal?

"""

from private import reddit_details
from psaw import PushshiftAPI
from typing import List, Coroutine
import asyncpraw
import datetime
import asyncio
from aiohttp import ClientSession, TCPConnector
from asyncprawcore import Requestor


class RedditData:

    def __init__(self):

        # official reddit api.
        # not constructing it because it will be created
        # inside async function. Else there's an error.
        # for more see self.get_sub_futures
        # self.reddit = asyncpraw.Reddit(**reddit_details)

        # unofficial reddit api with archived data
        self.pushshift = PushshiftAPI()

        # construct reddit details as well?
        # self.reddit_details = reddit_details

    # sometimes pushshiftAPI db has delay
    # check when was the last comment added
    def check_time_since_last_comment_in_pushshiftAPI(self) -> None:
        time = list(self.pushshift.search_comments(limit=1))
        dt = datetime.datetime
        time_diff = dt.now() - dt.fromtimestamp(time[0].created_utc)
        print(time_diff)

    def get_submission_ids(
            self,
            subreddit: str,
            before: int,
            after: int,
            **kwargs: any
    ) -> List[str]:
        """
        :param subreddit: subreddit name as on the app
        :param before: int (!) timestamp
        :param after: int (!) timestamp
        :return: list of ids
        """
        subs = self.pushshift.search_submissions(
                # me sure these are ints, as else the request will stall
                after=after,
                before=before,
                filter=['id'],
                subreddit=subreddit,
                # must include sort='asc' / sort='desc' as else will face this issue:
                # https://github.com/dmarx/psaw/issues/27
                sort='asc',
                **kwargs
        )
        return [sub.id for sub in subs]

    def get_submission_ids_new(self):
        pass

    @staticmethod
    async def _parse_single_submission(sub: Coroutine) -> List[dict]:
        """
        :param sub: coroutine to get a submission (asyncpraw.reddit.Submission) object
        :return: list of dicts with: where each item is submission + its comment/s
        """

        def parse_submission(submission: asyncpraw.reddit.Submission) -> dict:
            # parse the fields selected, we do not need all of it
            # [print("'", i, "'", ',', sep='') for i in dir(submission) if not i.startswith('_')]

            # TODO: consider _id field use in local db. Is the id field
            #  returned from reddit is unique? if yes, use it as _id.

            attrs = [
                'id',
                'created_utc',
                'subreddit_name_prefixed',
                'num_comments',
                'total_awards_received',
                'ups',
                'view_count',
                'title',
                'selftext',
            ]

            return {key: getattr(submission, key) for key in attrs}

        def parse_comment(comment: asyncpraw.reddit.Comment) -> dict:
            # parse the fields selected, we do not need all of it
            # [print("'", i, "'", ',', sep='') for i in dir(comment) if not i.startswith('_')]
            attrs = [
                'id',
                'link_id',
                'parent_id',
                'is_root',
                'depth',
                'created_utc',
                'total_awards_received',
                'ups',
                'body',
            ]

            return {key: getattr(comment, key) for key in attrs}

        # at this point, lets execute the coroutine
        # to get asyncpraw.reddit.Submission object
        sub = await sub

        sub_data = parse_submission(sub)
        comments = await sub.comments()
        if comments:

            # deal with comments that are not loaded into the request
            # this will result in additional network request
            # sometimes asyncprawcore.exceptions.RequestException
            # is raised. Catch it and try for a couple of times..?
            await comments.replace_more(limit=0)

            # comments.list() returns a list where all top level
            # comments are listed first, then second level comments and so on
            # this is not a problem as we can parse parent_id and depth level
            comments = comments.list()
            comments_ = [parse_comment(com) for com in await comments]

        return [sub_data] if not comments else [sub_data] + comments_

    def get_submissions(self, sub_ids: List[str], reddit_details: dict) -> list:

        async def get_sub_futures(sub_ids: List[str], reddit_details: dict) -> tuple:
            # make each sub_id request into a future and
            # gather all futures together into a tuple

            # have to include init of reddit object inside the async loop
            # else async loop raise an error. Should improve this fix :/

            # Something has to be done with Timeouts due to large
            # number of concurrent requests. Adding try/excepts on each
            # request in get_submission function seems lame. Maybe solve
            # this with custom client with limited concurrent connections:
            # aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10))
            reddit = asyncpraw.Reddit(
                **reddit_details,
                requestor_class=Requestor,
                requestor_kwargs={
                    'session': ClientSession(connector=TCPConnector(limit=5000))
                }
            )

            # use context or else the session above will not be closed
            # and warning/errors will pop for each request or session
            async with reddit:
                tasks = set()
                for id in sub_ids:
                    tasks.add(
                        asyncio.create_task(
                            self._parse_single_submission(reddit.submission(id))
                        )
                    )
                return await asyncio.gather(*tasks)

        # run all tasks and return list of results
        return asyncio.run(get_sub_futures(sub_ids, reddit_details))

    # combine methods to make a final function
    def get_data(
            self,
            start: datetime.datetime,
            end: datetime.datetime,
            subreddit: str
    ) -> List[List[dict]]:

        # returns List[List[dict]] where:
        # [[{sub}, {comm}, {comm} ..], [{sub}, {comm}, {comm} ...], ...]

        # Issues:
        # How to properly filter out removed/deleted submissions.
        # As of now, there seems to be no way to check if sub is
        # deleted/removed before the first request to PushshiftAPI
        # (get_submission_ids). This info is only available after
        # the second request to PRAW (get_submission).

        submission_params = {
            'subreddit': subreddit,
            'after': int(start.timestamp()),
            'before': int(end.timestamp()),
            'num_comments': '>5',  # 1st might be admin removal notice
        }

        # pull a list of submission ids from PushshiftAPI
        # pull submission details from PRAW
        sub_ids = self.get_submission_ids(**submission_params)
        data_batch = self.get_submissions(sub_ids, reddit_details)

        return data_batch

    def get_data_new(self, subreddit: str, limit: int = 1) -> List[List[dict]]:
        # this fn does not have the functionality
        # to filter submissions by time. It fetches X
        # newest submissions / comments.

        async def get_submissions_futures(subreddit: str, limit: int = 1) -> tuple:

            # cant get asyncpraw subreddit.new()
            # to return a coroutine not a submission,
            # so using the following wrapper
            async def _to_coroutine(sub):
                return sub

            reddit = asyncpraw.Reddit(**reddit_details)

            async with reddit:
                subreddit = await reddit.subreddit(subreddit)
                tasks = set()
                async for sub in subreddit.new(limit=limit):
                    # could simply await _parse_single_submission
                    # but then it will wait for each iter to finish.
                    # have to gather all the tasks and then execute
                    # asynchronously at once
                    tasks.add(
                        asyncio.create_task(
                            self._parse_single_submission(_to_coroutine(sub))
                        )
                    )
                return await asyncio.gather(*tasks)

        return asyncio.run(
            get_submissions_futures(subreddit=subreddit, limit=limit)
        )


def test_and_print() -> None:

    api = RedditData()
    data = api.get_data(
        start=datetime.datetime.now() - datetime.timedelta(hours=6),
        end=datetime.datetime.now(),
        subreddit='wallstreetbets',
    )

    new_data = api.get_data_new('satoshistreetbets', 5)
    print(new_data[0])

    # check some stats
    total = 0
    for sub in data:
        total += len(sub)
    print(f'# subs: {len(data)}, # subs+comments {total}', )

    # here we would push chunks to local db
    import json
    print(json.dumps(data[0][0], indent=4))
