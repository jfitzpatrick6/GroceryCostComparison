import pandas as pd

import aldis
import Walmart
import BJsScrapingTest
import tops


wmart = pd.read_csv("Walmart.csv")
bj = pd.read_csv("BJs.csv")
ald = pd.read_csv("Aldis.csv")
top = pd.read_csv("Tops.csv")

wmart = wmart.drop_duplicates()
bj = bj.drop_duplicates()
ald = ald.drop_duplicates()
top = top.drop_duplicates()

wmart['Store'] = "Walmart"
bj['Store'] = "BJs"
ald['Store'] = "Aldis"
top['Store'] = "Tops"

pd.concat([wmart, bj, ald, top]).to_csv("Cost report.csv", index=False)