import { IconEllipsis, IconSort } from '@posthog/icons'
import { IconArrowUp, IconArrowDown } from 'lib/lemon-ui/icons'
import { useActions, useValues } from 'kea'
import { useCallback, useMemo } from 'react'

import { Query } from '~/queries/Query/Query'
import {
    MarketingAnalyticsBaseColumns,
    ConversionGoalFilter,
    DataTableNode,
    MarketingAnalyticsTableQuery,
    NodeKind,
    MarketingAnalyticsHelperForColumnNames,
} from '~/queries/schema/schema-general'
import { QueryContext, QueryContextColumn } from '~/queries/types'
import { InsightLogicProps } from '~/types'
import { LemonMenu } from '@posthog/lemon-ui'

import { webAnalyticsDataTableQueryContext } from '../../../../../tiles/WebAnalyticsTile'
import { marketingAnalyticsLogic } from '../../logic/marketingAnalyticsLogic'
import { DynamicConversionGoalControls } from './DynamicConversionGoalControls'

interface MarketingAnalyticsTableProps {
    query: DataTableNode
    insightProps: InsightLogicProps
}

const QUERY_ORDER_BY_START_INDEX = 1

// TODO: refactor this component to support column actions (`...` button) to be more explicit on the different actions
// Also we need to centralize the column names and orderBy fields whether in the backend or frontend
export const MarketingAnalyticsTable = ({ query, insightProps }: MarketingAnalyticsTableProps): JSX.Element => {
    const { setMarketingAnalyticsOrderBy, clearMarketingAnalyticsOrderBy } = useActions(marketingAnalyticsLogic)
    const { marketingAnalyticsOrderBy, conversion_goals, dynamicConversionGoal } = useValues(marketingAnalyticsLogic)

    // Create a new query object with the orderBy field when sorting state changes
    const queryWithOrderBy = useMemo(() => {
        if (query.source.kind !== NodeKind.MarketingAnalyticsTableQuery) {
            return query
        }

        const source: MarketingAnalyticsTableQuery = {
            ...query.source,
            orderBy: marketingAnalyticsOrderBy ? [marketingAnalyticsOrderBy] : undefined,
        }

        return {
            ...query,
            source,
        }
    }, [query, marketingAnalyticsOrderBy])

    // Combined conversion goals - static from settings + dynamic goal
    const allConversionGoals = useMemo(() => {
        const goals: ConversionGoalFilter[] = []
        if (dynamicConversionGoal) {
            goals.push(dynamicConversionGoal)
        }
        if (conversion_goals) {
            goals.push(...conversion_goals)
        }
        return goals
    }, [conversion_goals, dynamicConversionGoal])

    const makeMarketingSortableCell = useCallback(
        (name: string, index: number) => {
            return function MarketingSortableCellComponent() {
                const [orderIndex, orderDirection] = marketingAnalyticsOrderBy || [null, null]
                const isSortedByMyField = orderIndex === index
                const isAscending = orderDirection === 'ASC'
                const isDescending = orderDirection === 'DESC'

                const menuItems = [
                    {
                        title: 'Sorting',
                        icon: <IconSort />,
                        items: [
                            {
                                label: 'Sort ascending',
                                icon: <IconArrowUp />,
                                onClick: () => setMarketingAnalyticsOrderBy(index, 'ASC'),
                                disabled: isSortedByMyField && isAscending,
                            },
                            {
                                label: 'Sort descending',
                                icon: <IconArrowDown />,
                                onClick: () => setMarketingAnalyticsOrderBy(index, 'DESC'),
                                disabled: isSortedByMyField && isDescending,
                            },
                            ...(isSortedByMyField
                                ? [
                                      {
                                          label: 'Clear sort',
                                          onClick: () => clearMarketingAnalyticsOrderBy(),
                                      },
                                  ]
                                : []),
                        ],
                    },
                ]

                return (
                    <LemonMenu items={menuItems}>
                        <span className="group cursor-pointer inline-flex items-center">
                            {name}
                            {isSortedByMyField ? (
                                isAscending ? (
                                    <>
                                        <IconArrowUp className="ml-1 group-hover:hidden" />
                                        <IconEllipsis className="ml-1 hidden group-hover:inline" />
                                    </>
                                ) : (
                                    <>
                                        <IconArrowDown className="ml-1 group-hover:hidden" />
                                        <IconEllipsis className="ml-1 hidden group-hover:inline" />
                                    </>
                                )
                            ) : (
                                <IconEllipsis className="ml-1 opacity-0 group-hover:opacity-100" />
                            )}
                        </span>
                    </LemonMenu>
                )
            }
        },
        [marketingAnalyticsOrderBy, setMarketingAnalyticsOrderBy, clearMarketingAnalyticsOrderBy]
    )

    const conversionGoalColumns = useMemo(() => {
        const columns: Record<string, QueryContextColumn> = {}

        allConversionGoals?.forEach((goal: ConversionGoalFilter, index: number) => {
            const goalName = goal.conversion_goal_name || `${MarketingAnalyticsHelperForColumnNames.Goal} ${index + 1}`
            const costPerGoalName = `${MarketingAnalyticsHelperForColumnNames.CostPer} ${goalName}`

            // Each conversion goal creates 2 columns (goal count + cost per goal), hence index * 2
            const goalColumnIndex =
                Object.keys(MarketingAnalyticsBaseColumns).length + index * 2 + QUERY_ORDER_BY_START_INDEX
            const costColumnIndex =
                Object.keys(MarketingAnalyticsBaseColumns).length + index * 2 + QUERY_ORDER_BY_START_INDEX + 1

            // Add conversion count column
            columns[goalName] = {
                renderTitle: makeMarketingSortableCell(goalName, goalColumnIndex),
                align: 'right',
            }

            // Add cost per conversion column
            columns[costPerGoalName] = {
                renderTitle: makeMarketingSortableCell(costPerGoalName, costColumnIndex),
                align: 'right',
            }
        })

        return columns
    }, [allConversionGoals, makeMarketingSortableCell])

    // Create custom context with sortable headers for marketing analytics
    const marketingAnalyticsContext: QueryContext = {
        ...webAnalyticsDataTableQueryContext,
        insightProps,
        columns: {
            ...webAnalyticsDataTableQueryContext.columns,
            // Indexes start at 1 because the orderBy field is 1-indexed e.g ORDER BY 1 DESC would sort by the first column
            ...Object.values(MarketingAnalyticsBaseColumns).reduce((acc, column, index) => {
                acc[column] = {
                    renderTitle: makeMarketingSortableCell(column, index + QUERY_ORDER_BY_START_INDEX),
                }
                return acc
            }, {} as Record<string, QueryContextColumn>),
            ...conversionGoalColumns,
        },
    }

    return (
        <div className="bg-surface-primary">
            <div className="p-4 border-b border-border bg-bg-light">
                <DynamicConversionGoalControls />
            </div>
            <Query query={queryWithOrderBy} readOnly={false} context={marketingAnalyticsContext} />
        </div>
    )
}
