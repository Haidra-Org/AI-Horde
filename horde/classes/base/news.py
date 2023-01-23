from datetime import datetime

class News:

    HORDE_NEWS = [
        {
            "date_published": "2023-01-23",
            "newspiece": "All workers must start sending the `bridge_agent` key in their job pop payloads. See API documentation.",
            "importance": "Workers"
        },
        {
            "date_published": "2022-10-10",
            "newspiece": "The [discord rewards bot](https://www.patreon.com/posts/new-kind-of-73097166) has been unleashed. Reward good contributions to the horde directly from the chat!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-09",
            "newspiece": "The horde now includes News functionality. Also [In the API!](/api/v2/status/news)",
            "importance": "Information"
        },
    ]

    def get_news(self):
        '''extensible function from gathering nodes from extensing classes'''
        return(self.HORDE_NEWS)

    def sort_news(self, raw_news):
        # unsorted_news = []
        # for piece in raw_news:
        #     piece_dict = {
        #         "date": datetime.strptime(piece["piece"], '%y-%m-%d'),
        #         "piece": piece["news"],
        #     }
        #     unsorted_news.append(piece_dict)
        sorted_news = sorted(raw_news, key=lambda p: datetime.strptime(p["date_published"], '%Y-%m-%d'), reverse=True)
        return(sorted_news)

    def sorted_news(self):
        return(self.sort_news(self.get_news()))