import pandas as pd

import AldisScrapingTest
import Walmart
import BJsScrapingTest


wmart = pd.read_csv("Walmart.csv")
bj = pd.read_csv("BJs.csv")
aldis = pd.read_csv("Aldis.csv")

wmart = wmart.drop_duplicates()
bj = bj.drop_duplicates()
aldis = aldis.drop_duplicates()

wmart['Store'] = "Walmart"
bj['Store'] = "BJs"
aldis['Store'] = "Aldis"

pd.concat([wmart, bj, aldis]).to_csv("Cost report.csv", index=False)