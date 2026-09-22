"""Simple Streamlit test dashboard."""
import streamlit as st

st.set_page_config(page_title="Test", layout="wide")
st.title("📊 Test Dashboard")
st.write("Hello from Streamlit!")
st.metric("Test Metric", "123", "+5%")
