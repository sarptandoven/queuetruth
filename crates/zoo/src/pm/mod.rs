//! Prediction-market queue models (`queuetruth.pm.queue`): the zoo's corrected mode on a prediction-market L2
//! series, run in one pass over the batches for several models at once. Prediction-market books have integer
//! fixed-point prices on a probability ladder, fractional sizes and a `clear` restatement at each day boundary, and
//! the models add `cancels_ahead` and the one-order-per-change (`atomic`) variants, so they are a sibling registry
//! (`PM_FAMILIES`, same shape as `FAMILIES`) and engine (`engine::run`) rather than rows of `FamilyDef`.

pub mod engine;
pub mod families;

pub use engine::{Fill, Model, OrderResult, Rows, SimOrder, run};
pub use families::{PM_FAMILIES, PmFamily, PmFamilyDef, pm_families_json};
