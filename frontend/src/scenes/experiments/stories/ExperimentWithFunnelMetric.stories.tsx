import { Meta, StoryObj } from '@storybook/react'
import { makeDelay } from 'lib/utils'
import { App } from 'scenes/App'
import { urls } from 'scenes/urls'

import { mswDecorator } from '~/mocks/browser'
import EXPERIMENT_WITH_FUNNEL_METRIC from '~/mocks/fixtures/api/experiments/experiment_with_funnel_metric.json'
import EXPOSURE_QUERY_RESULT from '~/mocks/fixtures/api/experiments/exposure_query_result.json'
import FUNNELS_METRIC_RESULT from '~/mocks/fixtures/api/experiments/funnel_metric_result.json'
import { NodeKind } from '~/queries/schema/schema-general'

const meta: Meta = {
    component: App,
    title: 'Scenes-App/Experiments',
    parameters: {
        layout: 'fullscreen',
        viewMode: 'story',
        mockDate: '2025-01-27',
        pageUrl: urls.experiment(EXPERIMENT_WITH_FUNNEL_METRIC.id),
    },
    decorators: [
        mswDecorator({
            get: {
                [`/api/projects/:team_id/experiments/${EXPERIMENT_WITH_FUNNEL_METRIC.id}/`]:
                    EXPERIMENT_WITH_FUNNEL_METRIC,
                [`/api/projects/:team_id/experiment_holdouts`]: [],
                [`/api/projects/:team_id/experiment_saved_metrics/`]: [],
                [`/api/projects/:team_id/feature_flags/${EXPERIMENT_WITH_FUNNEL_METRIC.feature_flag.id}/`]: {},
                [`/api/projects/:team_id/feature_flags/${EXPERIMENT_WITH_FUNNEL_METRIC.feature_flag.id}/status/`]: {},
            },
            post: {
                '/api/environments/:team_id/query': (req, res, ctx) => {
                    const body = req.body as Record<string, any>

                    if (body.query.kind === NodeKind.ExperimentExposureQuery) {
                        return res(ctx.json(EXPOSURE_QUERY_RESULT))
                    }

                    return res(ctx.json(FUNNELS_METRIC_RESULT))
                },
            },
        }),
    ],
}
export default meta

type Story = StoryObj<typeof meta>

// Small delay to ensure charts render completely
export const ExperimentWithFunnelMetric: Story = { play: makeDelay(500) }
