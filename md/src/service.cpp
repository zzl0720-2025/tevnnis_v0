#include "md/service.hpp"

namespace tevnnis::md {

grpc::Status MarketDataServiceImpl::PullDecisionBatch(grpc::ServerContext* /*context*/,
                                                      const tevnnis::PullRequest* request,
                                                      tevnnis::PullResponse* response) {
    *response = pipeline_.Pull(*request);
    return grpc::Status::OK;
}

grpc::Status MarketDataServiceImpl::WakeSignals(
    grpc::ServerContext* /*context*/, const tevnnis::WakeRequest* /*request*/,
    grpc::ServerWriter<tevnnis::WakeEvent>* /*writer*/) {
    // §4.3 wake model (v0 decision): pull-only. The RPC exists so the contract
    // is stable, but the rate-limited CRITICAL wake channel is deferred to v0.x.
    return grpc::Status(grpc::StatusCode::UNIMPLEMENTED,
                        "WakeSignals is deferred to v0.x; v0 is pull-only (DESIGN.md §4.3)");
}

}  // namespace tevnnis::md
