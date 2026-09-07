#pragma once
// MarketDataServiceImpl — the §4.3 gRPC surface of the md plane.
//
// A thin shell over MdPipeline: all behaviour (filtering, ordering, cursors,
// snapshots, dropped counts) lives in the pipeline and is tested without a
// server. WakeSignals is defined in the contract but deliberately not
// implemented in v0 — the wake model is pull-only (§4.3).

#include <grpcpp/grpcpp.h>

#include "md/pipeline.hpp"
#include "md_service.grpc.pb.h"

namespace tevnnis::md {

class MarketDataServiceImpl final : public tevnnis::MarketData::Service {
   public:
    // `pipeline` must outlive the service.
    explicit MarketDataServiceImpl(MdPipeline& pipeline) : pipeline_(pipeline) {}

    grpc::Status PullDecisionBatch(grpc::ServerContext* context,
                                   const tevnnis::PullRequest* request,
                                   tevnnis::PullResponse* response) override;

    grpc::Status WakeSignals(grpc::ServerContext* context, const tevnnis::WakeRequest* request,
                             grpc::ServerWriter<tevnnis::WakeEvent>* writer) override;

   private:
    MdPipeline& pipeline_;
};

}  // namespace tevnnis::md
