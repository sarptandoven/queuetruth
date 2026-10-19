"""Learned L2 fill model: discrete-time competing-risk hazard (SPEC 4.3 `queuetruth.model`, 5.5)."""

from __future__ import annotations

from queuetruth.model._common import model_feature_names
from queuetruth.model.card import assemble_card, fit_hazard
from queuetruth.model.climatology import Climatology
from queuetruth.model.hazard import HazardModel
from queuetruth.model.person_period import expand_person_period
from queuetruth.model.scoring import (
    CensoringKM,
    auc_at,
    c_index_uno,
    censoring_km,
    cif_from_hazards,
    ibs,
    ipcw_brier,
    ipcw_logloss,
    rcll,
)
from queuetruth.model.split import make_split, split_assignment

__all__ = [
    "CensoringKM",
    "Climatology",
    "HazardModel",
    "assemble_card",
    "auc_at",
    "c_index_uno",
    "censoring_km",
    "cif_from_hazards",
    "expand_person_period",
    "fit_hazard",
    "ibs",
    "ipcw_brier",
    "ipcw_logloss",
    "make_split",
    "model_feature_names",
    "rcll",
    "split_assignment",
]
