from horde.classes.base.news import News

class NewsExtended(News):

    STABLE_HORDE_NEWS = [
        {
            "date_published": "2023-02-20",
            "newspiece": "KoboldAI Horde has been merged into Stable Horde as a unified AI Horde!",
            "importance": "Information"
        },
        {
            "date_published": "2022-10-13",
            "newspiece": "KoboldAI Has been upgraded to the new countermeasures",
            "importance": "Information"
        },
    ]

    def get_news(self):
        return(super().get_news() + self.STABLE_HORDE_NEWS)
